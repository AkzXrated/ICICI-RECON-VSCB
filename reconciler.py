import pandas as pd
import numpy as np
import os
import re
import json
import math
import glob
import hashlib
import difflib
import time
import logging
import bisect
import storage
from typing import List, Dict, Optional, Any, Tuple, Set, Callable
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from lxml import etree

# NaN-safe JSON encoder
class SafeJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        return super().default(obj)

def safe_json_dumps(obj):
    """Serialize obj to JSON, replacing NaN/Inf floats with null."""
    return json.dumps(obj, cls=SafeJSONEncoder, default=str)

def clean_record(obj):
    """Recursively replace NaN/Inf floats with None for safe JSON output."""
    if isinstance(obj, dict):
        return {k: clean_record(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_record(i) for i in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj

def sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized sanitization of a DataFrame, replacing all NaNs/infs with None for safe JSON conversion."""
    if df.empty:
        return df
    df_clean = df.copy()
    df_clean = df_clean.replace([np.inf, -np.inf], np.nan)
    return df_clean.astype(object).where(pd.notnull(df_clean), None)

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_ROOT = os.path.join(BASE_DIR, 'Data', 'FY2025-26 - OLD')
if not os.path.exists(DEFAULT_DATA_ROOT):
    DEFAULT_DATA_ROOT = os.path.join(BASE_DIR, 'Data', 'Tallied', 'FY2025-26 - OLD')

ICICI_LEGENDS = {
    "BBPS": "Bharat Bill Payment Service", "BCTT": "Banking Cash Transaction Tax",
    "BIL": "Internet Bill payment / Third party", "BPAY": "Bill payment",
    "CCWD": "Cardless Cash Withdrawal", "DTAX": "Direct Tax",
    "EBA": "Transaction on ICICI Direct", "IDTX": "Indirect Tax",
    "IMPS": "Immediate Payment Service", "INF": "Internet fund transfer in linked accounts",
    "INFT": "Internal Fund Transfer (Within ICICI Bank)", "LCCBRN CMS": "Local cheque collection",
    "LNPY": "Linked loan payment", "MMT": "Mobile Money Transfer (IMPS)",
    "N chg": "NEFT Charges", "NEFT": "National Electronics Funds Transfer",
    "ONL": "Online Shopping transaction", "PAC": "Personal Accident cover",
    "PAVC": "Pay any Visa credit card", "PAYC": "Pay to Contact",
    "RCHG": "Recharge", "SMO": "Smart Money order", "T Chg": "Travel Charges",
    "TOP": "Mobile recharge", "UCCBRN CMS": "Upcountry cheque collection",
    "VAT": "Cash withdrawal (Other ATM)", "MAT": "Cash withdrawal (Other ATM)",
    "NFS": "Cash withdrawal (Other ATM)", "VPS": "Debit card transaction",
    "IPS": "Debit card transaction", "GIB": "Tax & Statutory payment",
    "CMS": "Internet bulk payment", "IPAY": "I-Pay Charges", "ECOL": "E-Collections Charges"
}

# --- UTILITIES ---
def get_paths(data_root: str) -> Dict[str, str]:
    base_data_dir = os.path.dirname(data_root)
    # Tallied FYs are archived one level deeper (Data/Tallied/FY...); the
    # global alias file always lives directly under Data/
    if os.path.basename(base_data_dir).lower() == 'tallied':
        base_data_dir = os.path.dirname(base_data_dir)
    return {
        'BANK_DIR': os.path.join(data_root, 'Bank Statement'),
        'GL_DIR': os.path.join(data_root, 'General Ledger'),
        'REPORTS_DIR': os.path.join(data_root, 'GL RTGS NEFT REPORT'),
        'CMS_DIR': os.path.join(data_root, 'CMS Report'),
        'UPI_DIR': os.path.join(data_root, 'UPI Collection Report'),
        'OUTPUT_JSON': os.path.join(data_root, 'data.json'),
        'MANUAL_LINKS_PATH': os.path.join(data_root, 'manual_links.json'),
        'ALIASES_PATH': os.path.join(base_data_dir, 'narrative_aliases.json')
    }

def parse_amount(amt: Any) -> float:
    if pd.isna(amt): return 0.0
    try:
        s = str(amt).replace(',', '').strip()
        if not s or s == '-': return 0.0
        try:
            v = float(s)
            return 0.0 if math.isnan(v) else v
        except ValueError:
            pass
        # Formatted exports ('Rs. 2,000.50', '12,000.00 CR', '(500.00)') used to
        # parse silently as 0 - a row that can never match. Direction comes
        # from the Cr/Dr column, so a CR/DR suffix is just stripped.
        neg = s.startswith('(') and s.endswith(')')
        s = re.sub(r'(?i)^(?:rs\.?|inr|₹)\s*|\s*(?:cr|dr)\.?$|[()\s]', '', s)
        return -float(s) if neg else float(s)
    except (ValueError, TypeError): return 0.0

def normalize_narration(text: str) -> str:
    if not isinstance(text, str): return ""
    return re.sub(r'^ICICI\s+BANK\s+THALASSERY\s*;?\s*', '', text.strip(), flags=re.IGNORECASE).strip()

def indic_phonetic_normalize(text: str) -> str:
    if not isinstance(text, str) or not text: return ""
    s = text.upper()
    s = re.sub(r'EE|EA|EY|IE', 'I', s)
    s = re.sub(r'OO|OU', 'U', s)
    s = re.sub(r'BH', 'B', s)
    s = re.sub(r'DH', 'D', s)
    s = re.sub(r'TH', 'T', s)
    s = re.sub(r'KH', 'K', s)
    s = re.sub(r'PH', 'F', s)
    s = re.sub(r'GH', 'G', s)
    s = re.sub(r'SH|ZH', 'S', s)
    s = re.sub(r'W', 'V', s)
    s = re.sub(r'Y(?=[AEIOU])', 'I', s)
    s = re.sub(r'([B-DF-HJ-NP-TV-Z])\1+', r'\1', s)
    return s

def clean_narration(text: str) -> str:
    if not isinstance(text, str): return ""
    # Strip leading DDMMYYYY or DD-MM-YYYY dates followed by spaces
    text = re.sub(r'^(?:\d{6,8}|\d{2}-\d{2}-\d{4})\s+', '', text.strip())
    # Strip gateway switch transaction tokens (e.g. AXIfe11c20c4561490eb2aa454e61c2d015, ICI..., SBI..., PTM..., HDF...)
    text = re.sub(r'\b(?:AXI|ICI|SBI|PTM|HDF|YES|BAR)[0-9a-zA-Z]{18,40}\b', ' ', text, flags=re.I)
    # Strip long hex switch hashes
    text = re.sub(r'\b[0-9a-fA-F]{20,40}\b', ' ', text)
    noise = ['BY', 'TO', 'CASH', 'TRANSFER', 'NEFT', 'RTGS', 'UPI', 'IMPS', 'CHEQUE', 'WITHDRAWAL', 'DEPOSIT', 'CHQ', 'THALASSERY', 'NFT', 'MMT']
    text = text.upper()
    text = re.sub(r'^.*?ICICI\s+BANK\s+THALASSERY\s*;?\s*', '', text)
    # Use word boundaries so words like 'AUTO', 'BABY', 'OCTOBER', 'CANTONMENT' are never mangled
    for w in noise:
        text = re.sub(rf'\b{w}\b', '', text)
    # Replace non-alphanumeric characters with spaces to preserve word boundaries
    text = re.sub(r'[^A-Z0-9]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

def edit_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return edit_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
        
    return previous_row[-1]

def has_near_match_utr(grow_refs, brow_refs) -> bool:
    g_12d = [r for r in grow_refs if re.fullmatch(r'\d{12}', r)]
    b_12d = [r for r in brow_refs if re.fullmatch(r'\d{12}', r)]
    if not g_12d or not b_12d:
        return False
    for g in g_12d:
        for b in b_12d:
            if edit_distance(g, b) <= 1:
                return True
    return False

# Channel / category classifier. Each rule is a word-boundary-anchored regex so
# customer names can never leak into tags (the old substring scan tagged
# 'SHEEBA' as EBA, 'PATTUVATHI' as VAT, 'RAHMATH' as MAT, 'JIPSA' as IPS ...).
# Order = display order. A narration may carry several tags (e.g. INWARD + NEFT).
TXN_TAG_RULES: List[Tuple[str, 're.Pattern']] = [
    ('UPI',     re.compile(r'(?:^|[\s/;:])U?PI/|/UPI/|\bUPI\b', re.I)),
    ('IMPS',    re.compile(r'\bMMT/IMPS\b|\bIMPS\b', re.I)),
    ('RTGS',    re.compile(r'\bRTGS\b', re.I)),
    ('NEFT',    re.compile(r'\bNEFT\b', re.I)),
    ('CMS',     re.compile(r'(?:^|[\s;])CMS/|\bCMS\d{8,}', re.I)),
    ('BBPS',    re.compile(r'\bBBPS\b|\bBIL/BPAY\b', re.I)),
    ('INFT',    re.compile(r'\bBIL/INFT\b|\bINFT\b', re.I)),
    ('TAX',     re.compile(r'(?:^|[\s;])GIB/|\b(?:GST|CGST|SGST|IGST|TDS|EPFO?|PF|ESIC?)\b', re.I)),
    ('CHARGES', re.compile(r'\b(?:IPAY|ECOL)[\s_]?CHGS?\b|\bCHGS?\b|\bCHARGES?\b|^[CS]GST\d{10,}', re.I)),
    ('CHEQUE',  re.compile(r'\b(?:CHQ|CHEQUE|CTS|CLG)\b|_CHQ_|-CHQ-', re.I)),
    ('CASH',    re.compile(r'\bCASH\b|^CAM/', re.I)),
    ('SELF',    re.compile(r'\bSELF\b', re.I)),
    ('INWARD',  re.compile(r'VDKU[O0]?\d{11,12}', re.I)),
]

def detect_type(text: str) -> str:
    n = str(text or '')
    found = [tag for tag, rx in TXN_TAG_RULES if rx.search(n)]
    return " + ".join(found) if found else 'OTHER'

def split_tags(txn_type: Any) -> List[str]:
    return [t for t in str(txn_type or '').split(' + ') if t and t != 'OTHER']

def extract_refs(text: str, stop_words: Optional[Set[str]] = None) -> List[str]:
    if not isinstance(text, str): return []
    
    found = []
    
    # 1. Surgically extract any 12-digit numeric sequences (standard UTRs) using boundary anchors
    # E.g. (?<!\d)\d{12}(?!\d) prevents slicing 14-16 digit account numbers into pseudo-UTRs
    raw_12d = re.findall(r'(?<!\d)\d{12}(?!\d)', text)
    for num in raw_12d:
        # Ignore 12-digit mobile numbers with 91 prefix unless explicitly part of a UPI string
        if re.fullmatch(r'91[6-9]\d{9}', num) and 'UPI' not in text.upper():
            continue
        if num not in found:
            found.append(num)
            
    # Pre-clean: Remove common UPI handles (e.g., user@okicici, 9876543210@ybl) to prevent false positives
    text_clean = re.sub(r'[A-Za-z0-9\.\-_]+@[A-Za-z0-9]+', '', text)
    # Pre-clean: Remove embedded UPI IDs in standard narrations (e.g., UPI/123456789012/UPI/username/)
    text_clean = re.sub(r'(UPI/\d{12}/(?:UPI/)?)[A-Za-z0-9\.\-_]+(/)', r'\1\2', text_clean, flags=re.I)
    
    # Include underscore and hash in token delimiters to prevent monster tokens like EGP_CHQ_03014270
    words = re.split(r'[\s/;,:\-@\.\(\)=\[\]\*\+\\\\_#]+', text_clean.upper())
    junk = {'THALASSERY', 'TRANSACTION', 'DESCRIPTION', 'TRANSFER', 'CMS', 'OKAXIS', 'OKICICI', 'OKHDFCBANK', 'OKSBI', 'UPI', 'INFO', 'REPN', 'REFNO', 'TRNS', 'IDTL', 'DATE', 'AMNT', 'ACNO', 'BRCH', 'PAYMENT', 'FROM', 'SENT', 'USING', 'BANK', 'KERALA', 'GRAMIN', 'STATE', 'INDIA', 'TELE', 'MOB', 'PHONE', 'VAL', 'FUND', 'DEBIT', 'CREDIT', 'CASH', 'CHQ', 'CHEQUE', 'NEFT', 'RTGS', 'IMPS'}
    if stop_words: junk.update(stop_words)
    
    for w in words:
        w = w.strip()
        if not w or w in junk: continue
        # Ignore bank branch IFSC codes (e.g. SBIN0000926, UTIB0000113)
        if re.fullmatch(r'[A-Z]{4}0[A-Z0-9]{6}', w): continue
        # Ignore long hex switch gateway hashes
        if re.fullmatch(r'[A-F0-9]{24,36}', w): continue
        if len(w) < 4 and w.isalpha(): continue
        if re.fullmatch(r'VDKU\d{12}', w):
            found.extend([f"{w[6:10]}-{w[10:].lstrip('0')}", w])
            continue
        if re.fullmatch(r'[A-Z]{2,6}\d{10,20}|\d{9,22}', w):
            # Guard against extracting 10-digit phone numbers as references
            if len(w) == 10 and re.fullmatch(r'[6-9]\d{9}', w):
                continue
            if w not in found: found.append(w)
            # A UTR glued to its channel prefix by a typo (e.g. 'UPI8630755138261')
            # also exposes the 11-13 digit tail so Tier 2.1 fuzzy-UTR can see it.
            m_tail = re.fullmatch(r'(?:UPI|IMPS|NEFT|RTGS|MMT)(\d{11,13})', w)
            if m_tail and m_tail.group(1) not in found:
                found.append(m_tail.group(1))
            continue
        if len(w) >= 4 and any(c.isalpha() for c in w) and any(c.isdigit() for c in w):
            if w not in found: found.append(w)
            continue
    return found

def extract_serial(text: str) -> Optional[str]:
    if not isinstance(text, str): return None
    # ICICI serials are usually 10-12 digits. Prefer explicit Ref No / Serial labels if present
    lbl = re.search(r'(?:Ref\s*No|Serial|Txn)\s*[:=\-]?\s*(\d{10,12})\b', text, re.I)
    if lbl:
        return lbl.group(1).zfill(12)
    m = re.search(r'\b(\d{10,12})\b', text)
    if m:
        return m.group(1).zfill(12)
    return None

def extract_cheque(text: str) -> Optional[str]:
    if not isinstance(text, str): return None
    # 1. Direct label with flexible delimiters including '_' and '#'
    m = re.search(r'\b(?:CHQ|CHEQUE|CH)[:\-\s_#]*(\d{6,10})', text, re.I)
    if m: return m.group(1)
    # 2. Embedded CHQ token (e.g. ERANHOLI_CHQ_0301427 or EGP-CHQ_03014267-RET)
    m = re.search(r'(?:CHQ|CHEQUE)[^0-9\s]{1,5}(\d{6,10})', text, re.I)
    if m: return m.group(1)
    # 3. Dedicated CTS/CLG clearing tokens
    m = re.search(r'\b(?:CTS|CLG)[:\-\s_#]*(\d{6,10})\b', text, re.I)
    if m: return m.group(1)
    return None

STATUTORY_SYNONYMS = {'EPFO': 'PF', 'EPF': 'PF', 'PROVIDENT': 'PF', 'ESIC': 'ESI'}

# Statutory payment classes. Bank pays these through GIB/ (ICICI tax gateway)
# mid-month; the GL often books them at month-end with free-form wording.
STATUTORY_CLASSES = {
    'PF':  re.compile(r'\b(?:E?PFO?|PROVIDENT\s+FUND)\b', re.I),
    'GST': re.compile(r'\b(?:GST|CGST|SGST|IGST|CPIN|GSTN)\b', re.I),
    'ESI': re.compile(r'\bESIC?\b', re.I),
    'TDS': re.compile(r'\b(?:TDS|ITNS\d*|OLTAS|INCOME\s*TAX)\b', re.I),
}

def statutory_class(narration: str) -> Optional[str]:
    n = str(narration or '')
    for cls, rx in STATUTORY_CLASSES.items():
        if rx.search(n):
            return cls
    return None

# IFSC bank-code prefix -> regex of the names a GL clerk writes for that bank.
# Used to pair inward 'RTGS-SBINR1...' bank credits with GL 'BY RTGS TRANSFER - SBI'.
IFSC_BANK_NAMES = {
    'SBIN': r'SBI|STATE\s+BANK', 'IBKL': r'IDBI', 'KSBK': r'KERALA\s+(?:STATE\s+CO[\s\-]?OP\w*\s+)?BANK|KSB|KERALA\s+BANK',
    'FDRL': r'FEDERAL', 'CNRB': r'CANARA', 'KLGB': r'KERALA\s+GRAMIN|KGB', 'HDFC': r'HDFC',
    'ICIC': r'ICICI', 'UTIB': r'AXIS', 'SIBL': r'SOUTH\s+INDIAN|SIB', 'CSBK': r'CSB|CATHOLIC\s+SYRIAN',
    'BKID': r'BANK\s+OF\s+INDIA|BOI', 'BARB': r'BARODA|BOB', 'PUNB': r'PUNJAB\s+NATIONAL|PNB',
    'UBIN': r'UNION\s+BANK', 'IOBA': r'INDIAN\s+OVERSEAS|IOB', 'IDIB': r'INDIAN\s+BANK',
    'KKBK': r'KOTAK', 'YESB': r'YES\s+BANK', 'IDFB': r'IDFC', 'ESAF': r'ESAF', 'DLXB': r'DHAN\s*LA?XMI',
    'CBIN': r'CENTRAL\s+BANK', 'MAHB': r'MAHARASHTRA', 'UCBA': r'UCO', 'INDB': r'INDUSIND',
    'TMBL': r'TAMILNAD\s+MERCANTILE|TMB', 'KVBL': r'KARUR|KVB', 'RATN': r'RBL', 'BDBL': r'BANDHAN',
}
_RTGS_BANK_IFSC_RX = re.compile(r'^(?:RTGS|NEFT)-([A-Z]{4})[A-Z0-9]', re.I)

def rtgs_ifsc_bank_matches(bank_narr: str, gl_narr: str) -> bool:
    """Bank 'RTGS-<IFSC4>...' credit whose IFSC bank is the bank named in an RTGS/NEFT GL line."""
    m = _RTGS_BANK_IFSC_RX.match(str(bank_narr or '').strip())
    if not m:
        return False
    g = str(gl_narr or '')
    if not re.search(r'\b(?:RTGS|NEFT)\b', g, re.I):
        return False
    names = IFSC_BANK_NAMES.get(m.group(1).upper())
    return bool(names and re.search(rf'\b(?:{names})\b', g, re.I))

def utr_suffix_match(gl_narr: str, bank_refs: List[str]) -> bool:
    """GL clerk typed only the UTR tail (e.g. 'UPI  29672' for 628827029672)."""
    tails = re.findall(r'(?<!\d)\d{5,6}(?!\d)', str(gl_narr or ''))
    if not tails:
        return False
    utrs = [r for r in (bank_refs or []) if len(r) == 12 and r.isdigit()]
    return any(u.endswith(t) for u in utrs for t in tails)

def narrations_compatible(n1: str, n2: str, aliases: Optional[Dict[str, List[str]]] = None) -> bool:
    if aliases:
        s1, s2 = clean_narration(n1), clean_narration(n2)
        if (s1 in aliases and s2 in aliases[s1]) or (s2 in aliases and s1 in aliases[s2]): return True
    s1, s2 = clean_narration(n1), clean_narration(n2)
    if s1 == s2 and s1 != "": return True
    
    # Check Indic phonetic equality on clean strings
    p1, p2 = indic_phonetic_normalize(s1), indic_phonetic_normalize(s2)
    if p1 == p2 and p1 != "": return True


    # Extract significant words (4+ chars) from cleaned + raw narrations
    w1, w2 = set(re.findall(r'[A-Z0-9]{4,}', s1)), set(re.findall(r'[A-Z0-9]{4,}', s2))
    w1.update(re.split(r'[\s/;,:\-@\._]+', n1.upper()))
    w2.update(re.split(r'[\s/;,:\-@\._]+', n2.upper()))
    
    # Whitelist high-value financial & accounting acronyms (2-3 chars)
    domain_acronyms = {'EPF', 'HKS', 'LIC', 'GP', 'GST', 'TDS', 'KSEB', 'PF'}
    w1.update({w for w in re.split(r'[\s/;,:\-@\._]+', n1.upper()) if w in domain_acronyms})
    w2.update({w for w in re.split(r'[\s/;,:\-@\._]+', n2.upper()) if w in domain_acronyms})
    
    w1 = {w.strip() for w in w1 if len(w.strip()) >= 4 or w.strip() in domain_acronyms}
    w2 = {w.strip() for w in w2 if len(w.strip()) >= 4 or w.strip() in domain_acronyms}
    # Statutory synonyms: GL 'PF OF EMPLOYEES' must meet bank 'GIB/EPFO'
    w1 |= {STATUTORY_SYNONYMS[w] for w in w1 if w in STATUTORY_SYNONYMS}
    w2 |= {STATUTORY_SYNONYMS[w] for w in w2 if w in STATUTORY_SYNONYMS}
    
    # Expanded noise: common banking words and local institution keywords that don't indicate a real match
    noise = {'OKAXIS', 'OKICICI', 'OKHDFCBANK', 'OKSBI', 'THALASSERY', 'BANK', 'ICICI',
             'ACCOUNT', 'PAYMENT', 'CREDIT', 'DEBIT', 'TRANSFER', 'TRANSACTION', 'BRANCH',
             'INDIA', 'KERALA', 'FROM', 'FUND', 'CHARGE', 'CHARGES', 'STATE', 'NATIONAL',
             'MOBILE', 'PHONE', 'DEPOSIT', 'CASH', 'OTHER', 'ONLINE', 'INTERNET', 'DIRECT',
             'SERVICE', 'AMOUNT', 'TOTAL', 'BALANCE', 'NUMBER', 'DATE', 'INWARD', 'OUTWARD',
             'RETURN', 'REVERSAL', 'CLEARING', 'COLLECTION', 'LIMIT', 'FEDERAL', 'COOPERATIVE',
             'PANCHAYATH', 'TALUK', 'GRAMIN', 'CANARA', 'SYNDICATE', 'AXIS', 'HDFC',
             'EMPLYEES', 'CO-OP', 'PUNJAB', 'SOUTH', 'UNION', 'BARODA', 'IDFC', 'CATHOL',
             'DHANLAXMI', 'ESAF', 'KOTAK', 'INDUSIND'}
    
    # Global Conflict Guard: Block match if both have 12-digit UTRs but they don't overlap
    g_12d = {w for w in w1 if re.fullmatch(r'\d{12}', w)}
    b_12d = {w for w in w2 if re.fullmatch(r'\d{12}', w)}
    if g_12d and b_12d and not (g_12d & b_12d):
        return False

    shared = {w for w in w1.intersection(w2) if w not in noise}
    if not shared:
        # Check phonetic overlap among non-noise words
        phonetic_w1 = {indic_phonetic_normalize(w) for w in w1 if w not in noise and len(w) >= 4}
        phonetic_w2 = {indic_phonetic_normalize(w) for w in w2 if w not in noise and len(w) >= 4}
        p_shared = phonetic_w1.intersection(phonetic_w2)
        if len(p_shared) >= 1:
            return True
        return False

    # Require at least one significant long match (7+ chars), a domain acronym, or 2+ shorter matches
    if any(w in domain_acronyms for w in shared):
        return True
    long_matches = [w for w in shared if len(w) >= 7]
    if long_matches: return True
    if len(shared) >= 2: return True
    
    # For single token match, require length >= 5 to prevent false matches on coincidental 4-letter words
    single_word = next(iter(shared))
    if len(single_word) >= 5:
        return True

    return False

BANK_NOISE = {
    'CANARA', 'KERALA', 'GRAMIN', 'STATE', 'INDIA', 'BANK', 'FEDERAL', 'COOPERATIVE',
    'HDFC', 'AXIS', 'ICICI', 'SBI', 'UPI', 'PUNJAB', 'NAT', 'NATIONAL', 'SOUTH',
    'INDIAN', 'IDFC', 'UNION', 'BARODA', 'CATHOL', 'CATHOLIC', 'DHANLAXMI', 'ESAF',
    'UCO', 'KOTAK', 'INDUSIND', 'CENTRAL', 'MAHARASHTRA', 'SYNDICATE', 'CORPORATION',
    'ALLAHABAD', 'ANDHRA', 'ORIENTAL', 'DENA', 'VIJAYA', 'YES', 'PAYTM', 'AIRTEL',
    'POST', 'IPPB', 'RBL', 'BANDHAN', 'IDBI', 'KVB', 'IOB', 'OVERSEAS', 'CITY'
}

def parse_upi_narration(narration: str) -> Dict[str, Optional[str]]:
    """
    Structured multi-schema UPI parser for ICICI Bank and GL narrations.
    Supports:
      - Type A (Standard 6-segment ICICI format):
          UPI / <12-digit UTR> / <Remark> / <UPI ID/VPA> / <Remitter Bank> / <Gateway Txn ID>
      - Type B (Extended 7-segment format):
          UPI / <Name/Remark> / <UPI ID/VPA> / <Marker> / <Remitter Bank> / <12-digit UTR> / <Gateway Txn ID>
      - Type C (5-segment GL export):
          UPI / <12-digit UTR> / UPI / <UPI ID/VPA> / <Remitter Bank>
      - Type D (3-segment GL concise):
          TO UPI / <12-digit UTR> / <UPI ID/VPA>
      - Type E (4-segment format):
          TO UPI / <12-digit UTR> / <Remark or Name> / <Bank or VPA>
    """
    empty_result = {'schema': None, 'name': None, 'upi_id': None, 'bank_name': None, 'utr': None, 'gateway_ref': None}
    if not isinstance(narration, str) or not narration.strip():
        return empty_result
    
    raw = narration.strip()
    clean = re.sub(r'^(?:TO|BY)\s*:?\s*', '', raw, flags=re.I).strip()
    parts = [p.strip() for p in clean.split('/')]
    first_upper = parts[0].upper()
    is_upi = ('UPI' in first_upper or first_upper == 'PI' or any('UPI' in p.upper() for p in parts) or '@' in raw)
    if not is_upi:
        return empty_result

    # Case 1: 7-part schema (Extended remitter schema)
    # e.g.: UPI / SREESHMA P / sreeshmaprasha / UPI / Punjab Nat / 658276117344 / AXIfe11c20c4561490eb2aa454e61c2d015
    # e.g.: UPI / AKSHAYA A / 9995295269@pty / Sent using / THE CATHOL / 300128993123 / PTM33ac99ff893542ff9de459cf8e31282f
    if len(parts) == 7:
        utr_cand = parts[5] if re.fullmatch(r'\d{12}', parts[5]) else None
        if not utr_cand and re.fullmatch(r'\d{12}', parts[1]):
            # 6-part with trailing empty delimiter or duplicate segment
            pass
        else:
            name_val = parts[1] if parts[1] else None
            upi_val = parts[2] if parts[2] else None
            bank_val = parts[4] if parts[4] else None
            gw_val = parts[6] if parts[6] else None
            return {
                'schema': '7_PART',
                'name': name_val,
                'upi_id': upi_val.upper().rstrip('@') if upi_val else None,
                'bank_name': bank_val,
                'utr': utr_cand,
                'gateway_ref': gw_val
            }

    # Case 2: 6-part schema (Standard ICICI UPI schema)
    # e.g.: UPI / 609408830153 / UPI / muhammadnabeeee / FEDERAL BANK / AXI4a061eeacc7e4b36805b14f613da07e3
    # e.g.: UPI / 121050731848 / UPI / mhdzaheervp@okh / South IndianBa / HDF17c697fdfb2f4be1b7fdc686a8209b9a
    if len(parts) == 6:
        utr_cand = parts[1] if re.fullmatch(r'\d{12}', parts[1]) else None
        upi_val = parts[3] if parts[3] else None
        bank_val = parts[4] if parts[4] else None
        gw_val = parts[5] if parts[5] else None
        return {
            'schema': '6_PART',
            'name': None,
            'upi_id': upi_val.upper().rstrip('@') if upi_val else None,
            'bank_name': bank_val,
            'utr': utr_cand,
            'gateway_ref': gw_val
        }

    # Case 3: 5-part schema (GL export without gateway ref)
    # e.g.: UPI / 609616226806 / UPI / BYJESHDIVYA@OKA / STATE BANK OF
    if len(parts) == 5:
        utr_cand = parts[1] if re.fullmatch(r'\d{12}', parts[1]) else None
        upi_val = parts[3] if parts[3] else None
        bank_val = parts[4] if parts[4] else None
        return {
            'schema': '5_PART',
            'name': None,
            'upi_id': upi_val.upper().rstrip('@') if upi_val else None,
            'bank_name': bank_val,
            'utr': utr_cand,
            'gateway_ref': None
        }

    # Case 4: 3-part schema (concise GL entry)
    # e.g.: TO UPI / 646076518484 / REGALTRUST802
    if len(parts) == 3:
        utr_cand = parts[1] if re.fullmatch(r'\d{12}', parts[1]) else None
        upi_val = parts[2] if parts[2] else None
        return {
            'schema': '3_PART',
            'name': None,
            'upi_id': upi_val.upper().rstrip('@') if upi_val else None,
            'bank_name': None,
            'utr': utr_cand,
            'gateway_ref': None
        }

    # Case 5: 4-part schema
    # e.g.: TO UPI / 653474328626 / MUHAMMED K K / BANK OF BARODA
    # e.g.: BY UPI / 622801260467 / PAID VIA CRED / 7736055264@AXIX
    if len(parts) == 4:
        utr_cand = parts[1] if re.fullmatch(r'\d{12}', parts[1]) else None
        if '@' in parts[3]:
            return {
                'schema': '4_PART',
                'name': None,
                'upi_id': parts[3].upper().rstrip('@'),
                'bank_name': None,
                'utr': utr_cand,
                'gateway_ref': None
            }
        else:
            return {
                'schema': '4_PART',
                'name': parts[2] if parts[2] else None,
                'upi_id': None,
                'bank_name': parts[3] if parts[3] else None,
                'utr': utr_cand,
                'gateway_ref': None
            }

    # Fallback: Extract 12-digit UTR if present and VPA with @
    found_utr = None
    for p in parts:
        m_utr = re.search(r'(?<!\d)\d{12}(?!\d)', p)
        if m_utr:
            found_utr = m_utr.group(0)
            break
    m_vpa = re.search(r'([A-Za-z0-9\.\-_]{3,}@[A-Za-z0-9\.\-_]*)', raw)
    found_vpa = m_vpa.group(1).upper().rstrip('@') if m_vpa else None

    return {
        'schema': 'OTHER',
        'name': None,
        'upi_id': found_vpa,
        'bank_name': None,
        'utr': found_utr,
        'gateway_ref': None
    }

def extract_upi_id(narration: str) -> Optional[str]:
    if not isinstance(narration, str): return None
    
    # 1. First check structured multi-schema parser
    parsed = parse_upi_narration(narration)
    cand = parsed.get('upi_id')
    if cand:
        c = cand.strip().upper().rstrip('@')
        if c not in BANK_NOISE and len(c) >= 3:
            return c

    # 2. Check for explicit VPA with '@'
    # Restrict user part to non-whitespace characters to prevent greedy string captures
    m = re.search(r'([A-Za-z0-9\.\-_]{3,}@[A-Za-z0-9\.\-_]*)', narration)
    if m:
        val = m.group(1).strip().upper().rstrip('@')
        if val not in BANK_NOISE and len(val) >= 3:
            return val

    # 3. Check for UPI path: UPI/12digits/UPI/username or UPI/12digits/username
    m = re.search(r'UPI/\d{12}/(?:UPI/)?([A-Za-z0-9\.\-_]+)', narration, re.I)
    if m:
        val = m.group(1).strip().upper()
        if val not in BANK_NOISE and len(val) >= 3:
            return val

    # 4. Fallback: Check for just /UPI/ followed by username
    m = re.search(r'/UPI/([A-Za-z0-9\.\-_]+)', narration, re.I)
    if m:
        val = m.group(1).strip().upper()
        if val not in BANK_NOISE and len(val) >= 3:
            return val

    return None

def vpa_matches(v1: Optional[str], v2: Optional[str]) -> bool:
    """
    Mask-aware VPA matcher supporting symmetric and asymmetric matching.
    e.g., SABITHATLY2000@OKSBI matches XX2000@OKSBI (tail '2000' >= 3 chars, same domain).
    """
    if not v1 or not v2: return False
    v1_u, v2_u = str(v1).upper().strip().rstrip('@'), str(v2).upper().strip().rstrip('@')
    if not v1_u or not v2_u: return False
    if v1_u == v2_u: return True
    
    u1, d1 = v1_u.split('@', 1) if '@' in v1_u else (v1_u, '')
    u2, d2 = v2_u.split('@', 1) if '@' in v2_u else (v2_u, '')
    
    if d1 and d2 and d1 == d2:
        if u1 == u2: return True
        if u1.startswith('XX') and not u2.startswith('XX'):
            tail = u1[2:]
            if len(tail) >= 3 and u2.endswith(tail): return True
        elif u2.startswith('XX') and not u1.startswith('XX'):
            tail = u2[2:]
            if len(tail) >= 3 and u1.endswith(tail): return True
    elif not d1 or not d2:
        if u1 == u2: return True
        if u1.startswith('XX') and not u2.startswith('XX'):
            tail = u1[2:]
            if len(tail) >= 3 and u2.endswith(tail): return True
        elif u2.startswith('XX') and not u1.startswith('XX'):
            tail = u2[2:]
            if len(tail) >= 3 and u1.endswith(tail): return True

    return False


def compute_duplicates(df: pd.DataFrame, global_upi_registry: Optional[Set[str]] = None) -> pd.DataFrame:
    if df.empty:
        return df
    
    # Reset/Initialize columns
    df['duplicate_index'] = None
    df['duplicate_ref'] = None
    df['is_duplicate'] = False
    df['is_duplicate_matched'] = False
    if 'upi_id' not in df.columns:
        df['upi_id'] = None
    if 'party_name' not in df.columns:
        df['party_name'] = None
    if 'bank_name' not in df.columns:
        df['bank_name'] = None
    df['has_repeats'] = False
    df['cross_fy_regular'] = False
    
    # Structured UPI parsing & extraction
    parsed_series = df['narration'].apply(parse_upi_narration)
    extracted_upis = df['narration'].apply(extract_upi_id)
    if 'upi_id' not in df.columns:
        df['upi_id'] = extracted_upis
    else:
        df['upi_id'] = df['upi_id'].fillna(extracted_upis)

    parsed_names = parsed_series.apply(lambda p: p.get('name'))
    if 'party_name' not in df.columns:
        df['party_name'] = parsed_names
    else:
        df['party_name'] = df['party_name'].fillna(parsed_names)

    parsed_banks = parsed_series.apply(lambda p: p.get('bank_name'))
    if 'bank_name' not in df.columns:
        df['bank_name'] = parsed_banks
    else:
        df['bank_name'] = df['bank_name'].fillna(parsed_banks)
    if isinstance(global_upi_registry, dict):
        def fill_party_name(row):
            p = row.get('party_name')
            if pd.notna(p) and str(p).strip() != '' and str(p).strip().upper() not in ('NONE', 'NAN'):
                return p
            u = row.get('upi_id')
            if u and u in global_upi_registry:
                entry = global_upi_registry[u]
                if isinstance(entry, dict) and entry.get('party_name'):
                    return entry['party_name']
                elif isinstance(entry, str) and entry.strip():
                    return entry.strip().upper()
            return p
        df['party_name'] = df.apply(fill_party_name, axis=1)
    
    # Count UPI occurrences
    upi_counts = {}
    valid_upis = df['upi_id'].dropna()
    for upi in valid_upis:
        if upi != '':
            upi_counts[upi] = upi_counts.get(upi, 0) + 1
            
    # Tag regular customers (seen more than once in current year OR in global registry)
    def check_regular(u):
        if not u: return False
        # Do not mark unresolved short masked handles (e.g. XX36-1 or XX2000) as regular customers
        # because masking causes distinct customers to share identical short tokens
        if str(u).startswith('XX') and len(str(u).split('@')[0]) <= 6:
            return False
        return bool(upi_counts.get(u, 0) > 1 or (global_upi_registry and u in global_upi_registry))
        
    def check_cross_fy(u):
        if not u or not global_upi_registry: return False
        return u in global_upi_registry

    df['has_repeats'] = df['upi_id'].apply(check_regular)
    df['cross_fy_regular'] = df['upi_id'].apply(check_cross_fy)
    
    # Ensure sorted by dt
    df = df.sort_values(by='dt').reset_index(drop=True)
    
    m = df['ref_id'].notna() & (df['ref_id'] != '')
    if not m.any():
        return df
        
    # Mark duplicate_ref and is_duplicate using pandas
    df.loc[m & df.duplicated(subset=['ref_id', 'amount', 'type_sign'], keep=False), 'duplicate_ref'] = df['ref_id']
    df.loc[m & df.duplicated(subset=['ref_id', 'amount', 'type_sign'], keep='first'), 'is_duplicate'] = True
    
    # Group by ['ref_id', 'amount', 'type_sign'] to set indices and check matched partner
    dup_groups = df[m & df.duplicated(subset=['ref_id', 'amount', 'type_sign'], keep=False)]
    matched_keys = set()
    
    for (ref_id, amt, sign), group in dup_groups.groupby(['ref_id', 'amount', 'type_sign']):
        if group['matched'].any():
            matched_keys.add((ref_id, amt, sign))
            
        sorted_indices = group.index
        for i, idx in enumerate(sorted_indices, 1):
            df.at[idx, 'duplicate_index'] = i
            df.at[idx, 'duplicate_ref'] = ref_id
            
    # Set is_duplicate_matched
    def check_dup_matched(row):
        if not row['matched'] and pd.notna(row['duplicate_index']):
            key = (row['duplicate_ref'], row['amount'], row['type_sign'])
            if key in matched_keys:
                return True
        return False
        
    df['is_duplicate_matched'] = df.apply(check_dup_matched, axis=1)

    return df

def refresh_duplicate_matched(df: pd.DataFrame) -> pd.DataFrame:
    """Recompute only the matched-dependent duplicate flag. The structural
    columns from compute_duplicates (upi_id, has_repeats, duplicate_index,
    duplicate_ref, ...) are unaffected by matching and need no re-run."""
    if df.empty or 'duplicate_index' not in df.columns:
        return df
    df['is_duplicate_matched'] = False
    m = df['duplicate_index'].notna()
    if not m.any():
        return df
    grp_matched = df[m].groupby(['duplicate_ref', 'amount', 'type_sign'])['matched'].transform('any')
    df.loc[m, 'is_duplicate_matched'] = grp_matched & ~df.loc[m, 'matched']
    return df

def calculate_confidence_score(grow: Dict, brow: Dict) -> Tuple[int, str, str]:
    # Extract references and UPI IDs
    g_refs = set(grow.get('all_refs', []))
    b_refs = set(brow.get('all_refs', []))
    
    g_upi = extract_upi_id(grow['narration'])
    b_upi = extract_upi_id(brow['narration'])
    
    # 1. Exact Ref Number Match
    common_refs = g_refs & b_refs
    if common_refs:
        ref_val = list(common_refs)[0]
        return 100, f"Ref number matched completely ({ref_val}) - 100% confidence", "Ref Match"
        
    # 2. High-similarity Ref Typo (Edit distance on UTRs)
    g_12d = [r for r in grow.get('all_refs', []) if len(r) >= 10 and r.isdigit()]
    b_12d = [r for r in brow.get('all_refs', []) if len(r) >= 10 and r.isdigit()]
    for gr in g_12d:
        for br in b_12d:
            ratio = difflib.SequenceMatcher(None, gr, br).ratio()
            if ratio >= 0.8:
                return 85, f"Possible Ref Typo: {gr} vs {br} - 85% confidence", "Possible Typo"
                
    # 3. UPI ID Match without reference match
    days = abs((brow['dt'] - grow['dt']).days)
    if g_upi and b_upi:
        # Clean both UPI IDs to extract only letters and numbers (remove handles, spaces, and UTRs)
        g_clean = re.sub(r'@[A-Z0-9\.\-_]+', '', g_upi.upper()) # Remove handle e.g. @OKAXIS
        b_clean = re.sub(r'@[A-Z0-9\.\-_]+', '', b_upi.upper())
        
        # Remove 12-digit UTR sequences from usernames to avoid mixing references with names
        g_clean = re.sub(r'\d{12}', '', g_clean)
        b_clean = re.sub(r'\d{12}', '', b_clean)
        
        # Remove non-alphanumeric noise and spaces
        g_clean = re.sub(r'[^A-Z0-9]', '', g_clean)
        b_clean = re.sub(r'[^A-Z0-9]', '', b_clean)
        
        if g_clean and b_clean:
            # Exact clean username match or substring containment (e.g. SHEEBAPANIYAN12 vs SHEEBAPANIYAN123)
            if g_clean == b_clean or (len(g_clean) >= 6 and len(b_clean) >= 6 and (g_clean in b_clean or b_clean in g_clean)):
                score = max(55, 80 - (days * 4))
                return int(score), f"UPI ID matched fuzzy ({g_upi} vs {b_upi}) - {score}% confidence", "UPI Match"
            
            # Fuzzy SequenceMatcher ratio match on usernames
            upi_ratio = difflib.SequenceMatcher(None, g_clean, b_clean).ratio()
            if upi_ratio >= 0.85:
                score = max(50, 75 - (days * 5))
                return int(score), f"Fuzzy UPI Match ({round(upi_ratio*100)}% similarity) - {score}% confidence", "UPI Match"
        
    # 4. Standard Narration Similarity & Date closeness (Capped at 70%)
    narr_sim = round(difflib.SequenceMatcher(None, clean_narration(grow['narration']), clean_narration(brow['narration'])).ratio() * 100)
    score = max(0, min(70, narr_sim))
    score = max(10, score - (days * 3))
    
    guide = f"Narration similarity: {narr_sim}% ({days} days apart)"
    label = "Narrative Match" if narr_sim > 50 else "Amt Match"
    
    return int(score), guide, label


# --- DATA LOADERS ---
def _load_single_bank_psv(f: str) -> List[Dict[str, Any]]:
    rows = []
    try:
        df = pd.read_csv(f, sep='|', skiprows=7)
        df.columns = [c.strip() for c in df.columns]
        for _, r in df.iterrows():
            try:
                amt = parse_amount(r.get('Transaction Amount(INR)', r.get('Amount', 0)))
                if amt == 0: continue
                id_val = str(r.get('Transaction ID', r.get('Reference No', ''))).strip()
                if id_val == 'nan' or not id_val: continue
                
                dt_str = str(r.get('Value Date', r.get('Transaction Date', ''))).strip()
                try:
                    dt = pd.to_datetime(dt_str, dayfirst=True)
                except:
                    dt = pd.to_datetime(dt_str) # Fallback to auto-detect
                
                narr = str(r.get('Description', r.get('Narration', ''))).strip()
                refs = extract_refs(narr)
                
                # Check dedicated ChequeNo. column from PSV first before regex fallback
                chq_col = str(r.get('ChequeNo.', '') or '').strip()
                if chq_col and chq_col.isdigit() and len(chq_col) >= 6:
                    chq_no = chq_col
                else:
                    chq_no = extract_cheque(narr)

                rows.append({
                    'bank_id': id_val, 'date': dt.strftime('%d-%m-%Y'), 'dt': dt,
                    'narration': narr, 'amount': round(amt, 2), 
                    'type_sign': 'DEBIT' if str(r.get('Cr/Dr', '')).upper() == 'DR' else 'CREDIT',
                    'txn_type': detect_type(narr), 'ref_id': refs[0] if refs else None,
                    'all_refs': refs, 'cheque_no': chq_no
                })
            except Exception as row_err:
                logger.warning(f"Skipping row in {f}: {row_err}")
    except Exception as e: logger.error(f"Error loading {f}: {e}")
    return rows

def load_bank_psv(data_root: str) -> pd.DataFrame:
    paths = get_paths(data_root)
    empty_cols = ['bank_id', 'date', 'dt', 'narration', 'amount', 'type_sign', 'txn_type', 'ref_id', 'all_refs', 'cheque_no']
    files = glob.glob(os.path.join(paths['BANK_DIR'], "*.psv"))
    if not files: return pd.DataFrame(columns=empty_cols)
    rows = []
    max_workers = min(4, len(files))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(_load_single_bank_psv, files)
        for res in results:
            rows.extend(res)
            
    if not rows: return pd.DataFrame(columns=empty_cols)
    
    df_bank = pd.DataFrame(rows)
    df_bank = df_bank.drop_duplicates(subset=['bank_id', 'narration', 'amount'])
    
    # Make bank_id unique sequentially
    bank_id_counts = {}
    def make_unique(row):
        base = row['bank_id']
        count = bank_id_counts.get(base, 0)
        bank_id_counts[base] = count + 1
        return base if count == 0 else f"{base}_{count+1}"
        
    df_bank['bank_id'] = df_bank.apply(make_unique, axis=1)
    return df_bank

def _load_single_rtgs_report(f: str) -> List[Dict[str, Any]]:
    reports = []
    try:
        for node in etree.parse(f).getroot().xpath('.//G_CASH_ADJ'):
            crn = node.findtext('CRN_NUM') or node.findtext('RTGS_TRAN_ID')
            if not crn: continue
            try:
                dt = pd.to_datetime(node.findtext('PAYMENT_DATE') or node.findtext('RTGS_DATE1') or node.findtext('DAY_DT'), dayfirst=True)
            except: continue
            serial = node.findtext('SERIAL_NO')
            if not serial:
                desc = node.findtext('RTGS_DESC')
                if desc:
                    m = re.search(r'Txn=(\d+)', desc)
                    if m: serial = m.group(1)
            
            # Extract inward routing account key from TO_CBS_ACCNO if present (e.g. 031970003804 -> 1970-3804)
            acc_node = node.findtext('TO_CBS_ACCNO')
            acc_key = None
            if acc_node and len(acc_node.strip()) >= 10:
                ac = acc_node.strip()
                acc_key = f"{ac[6:10]}-{ac[10:].lstrip('0')}"

            reports.append({
                'date': dt.strftime('%d-%m-%Y'), 'amount': parse_amount(node.findtext('PAY_AMOUNT') or node.findtext('RTGS_AMOUNT')),
                'crn': crn, 'name': (node.findtext('BENEF_NAME') or node.findtext('REMIT_NAME') or node.findtext('RTGS_DESC') or '').upper(),
                'serial': str(serial).strip().zfill(12) if serial and str(serial).strip() != '' else None,
                'acc_key': acc_key
            })
    except: pass
    return reports

def load_rtgs_reports(data_root: str) -> List[Dict[str, Any]]:
    paths = get_paths(data_root)
    files = glob.glob(os.path.join(paths['REPORTS_DIR'], "*.xml"))
    if not files: return []
    reports = []
    max_workers = min(4, len(files))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(_load_single_rtgs_report, files)
        for res in results:
            reports.extend(res)
    return reports

def _load_single_cms_report(f: str) -> List[Dict[str, Any]]:
    cms_data = []
    try:
        df = None
        try:
            df_temp = pd.read_csv(f, sep='\t', nrows=20)
            if any('UTR' in str(col).upper() for col in df_temp.columns): df = pd.read_csv(f, sep='\t')
        except: pass
        
        if df is None:
            df_probe = pd.read_excel(f, nrows=20, header=None)
            header_row = next((i for i, row in df_probe.iterrows() if any(x in [str(v).upper() for v in row if pd.notna(v)] for x in ['UTR NO', 'UTR NO.', 'UTR', 'PAYMENT REF NO'])), 0)
            df = pd.read_excel(f, skiprows=header_row)
        
        if df is not None:
            df.columns = [str(c).strip() for c in df.columns]

            # Resolve candidate columns once per file (was a per-row, per-key column scan)
            def cols_for(keys):
                out = []
                for k in keys:
                    out.extend(c for c in df.columns if k.upper() in c.upper() and c not in out)
                return out
            utr_cols = cols_for(['UTR NO', 'Instrument UTR No', 'UTR'])
            serial_cols = cols_for(['Instrument_No', 'Instrument Ref No', 'Serial No'])
            cmsref_cols = cols_for(['Payment Ref No', 'Instrument Ref No'])
            custref_cols = cols_for(['Customer Ref No'])
            benef_cols = cols_for(['Beneficiary Name'])
            benacc_cols = cols_for(['Beneficiary A/c No', 'Beneficiary Account'])
            amt_cols = [c for c in df.columns if c.strip().upper() in ('AMOUNT', 'PAYMENT AMOUNT', 'TRANSACTION AMOUNT')]
            date_cols = [c for c in df.columns if c.strip().upper() in ('DATE', 'PAYMENT DATE', 'VALUE DATE')]
            status_cols = [c for c in df.columns if c.strip().upper() == 'STATUS']
            mode_cols = cols_for(['Payment Mode'])
            ifsc_cols = cols_for(['IFSC'])

            for r in df.to_dict('records'):
                try:
                    utr_raw = _cell(r, utr_cols)
                    if not utr_raw: continue
                    # Only real references count. Status text that lands in the
                    # UTR column ('Final Status Awaited', 'Credit') used to be
                    # injected as a bogus 'FINAL'/'CREDIT' reference.
                    utr_match = re.search(r'([A-Z]{2,6}\d{10,20}|\d{12})', utr_raw)
                    utr = utr_match.group(1) if utr_match else None

                    serial_raw = _cell(r, serial_cols)
                    serial = serial_raw.split('.')[0].strip().zfill(12) if serial_raw else None

                    cms_ref_raw = _cell(r, cmsref_cols)
                    cms_ref = cms_ref_raw.split('_')[0].strip().upper() if cms_ref_raw else None

                    cust_ref = _cell(r, custref_cols)
                    if cust_ref and re.fullmatch(r'\d+\.0', cust_ref):
                        cust_ref = cust_ref[:-2]
                    try:
                        cdt = pd.to_datetime(_cell(r, date_cols), dayfirst=True)
                        cdt = None if pd.isna(cdt) else cdt
                    except (ValueError, TypeError):
                        cdt = None

                    cms_data.append({
                        'cms_ref': cms_ref,
                        'serial': serial,
                        'utr': utr,
                        'utr_raw': utr_raw,
                        'cust_ref': cust_ref.upper() if cust_ref else None,
                        'benef_name': (_cell(r, benef_cols) or '').upper(),
                        'benef_acc': (_cell(r, benacc_cols) or '').lstrip("'").strip() or None,
                        'amount': parse_amount(_cell(r, amt_cols)),
                        'dt': cdt,
                        'status': (_cell(r, status_cols) or '').upper(),
                        'mode': (_cell(r, mode_cols) or '').upper(),
                        'ifsc': (_cell(r, ifsc_cols) or '').upper() or None,
                        'source': os.path.basename(f),
                    })
                except Exception as row_err:
                    logger.debug(f"Skipping CMS row in {f}: {row_err}")
    except Exception as e: logger.error(f"Error loading CMS {f}: {e}")
    return cms_data

def _cell(row: Dict[str, Any], cols: List[str]) -> Optional[str]:
    """First non-empty value among `cols` in a record dict, as a stripped string."""
    for c in cols:
        v = row.get(c)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        s = str(v).strip()
        if s and s.lower() not in ('nan', 'none', 'nat'):
            return s
    return None

# --- DOUBLE-TRANSFER FAIL-SAFE ---
_PAID_RX = re.compile(r'\bPAID\b|SUCCESS|CREDITED|EXECUTED', re.I)
_PENDING_RX = re.compile(r'AWAIT|PENDING|PROCESS|INITIATED|SENT|QUEUE', re.I)
_FAILED_RX = re.compile(r'FAIL|REJECT|RETURN|CANCEL|REVERS', re.I)

def _payment_state(c: Dict[str, Any]) -> str:
    text = f"{c.get('status', '')} {c.get('utr_raw', '')}"
    if _FAILED_RX.search(text): return 'FAILED'
    if _PAID_RX.search(text): return 'PAID'
    if _PENDING_RX.search(text): return 'PENDING'
    return 'UNKNOWN'

def detect_transfer_alerts(cms: List[Dict[str, Any]], window_days: int = 7) -> List[Dict[str, Any]]:
    """Find outward payments sent twice to the same beneficiary account for the
    same amount within `window_days` — typically a transfer stuck at 'Final Status
    Awaited' that was manually re-sent. These are NEVER auto-matched; they are
    flagged so the original can be checked before it also goes through.
    Levels: DOUBLE_PAID (>=2 legs paid: money left twice), RETRY_PENDING (one
    leg paid, another still pending: may still double-pay), RETRY_OK (other legs
    failed: safe, informational)."""
    seen, rows = set(), []
    for c in cms:
        if not c.get('benef_acc') or not c.get('amount') or c.get('dt') is None:
            continue
        key = (c.get('cms_ref'), c.get('serial'), c.get('cust_ref'), c['amount'])
        if key in seen:  # the same row repeated across overlapping monthly reports
            continue
        seen.add(key)
        rows.append(c)

    by_key: Dict[Tuple[str, float], List[Dict]] = {}
    for c in rows:
        by_key.setdefault((c['benef_acc'], round(c['amount'], 2)), []).append(c)

    alerts = []
    for (acc, amt), lst in by_key.items():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda x: x['dt'])
        cluster = [lst[0]]
        clusters = []
        for c in lst[1:]:
            if (c['dt'] - cluster[-1]['dt']).days <= window_days:
                cluster.append(c)
            else:
                clusters.append(cluster); cluster = [c]
        clusters.append(cluster)
        for cl in clusters:
            if len({x.get('cms_ref') or x.get('cust_ref') for x in cl}) < 2:
                continue
            states = [_payment_state(x) for x in cl]
            n_paid, n_pending = states.count('PAID'), states.count('PENDING') + states.count('UNKNOWN')
            if n_paid >= 2:
                level = 'DOUBLE_PAID'   # demoted/kept by apply_transfer_alerts' GL-voucher check
            elif n_pending >= 1:
                level = 'RETRY_PENDING'
            else:
                level = 'RETRY_OK'
            gid = hashlib.md5(f"{acc}|{amt}|{cl[0]['dt']:%Y%m%d}".encode()).hexdigest()[:10]
            alerts.append({
                'id': gid, 'level': level, 'benef_acc': acc, 'amount': amt,
                'benef_name': cl[0].get('benef_name'),
                'legs': [{'cms_ref': x.get('cms_ref'), 'serial': x.get('serial'), 'cust_ref': x.get('cust_ref'),
                          'utr': x.get('utr'), 'ifsc': x.get('ifsc'), 'date': x['dt'].strftime('%d-%m-%Y'), 'mode': x.get('mode'),
                          'status': x.get('status') or x.get('utr_raw'), 'state': s}
                         for x, s in zip(cl, states)],
            })
    return alerts

def apply_transfer_alerts(df_bank: pd.DataFrame, df_gl: pd.DataFrame, alerts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only genuine re-send groups and tag their GL rows (via serial / CMS
    ref) and bank rows (via UTR / customer ref / beneficiary IFSC) with a
    `transfer_alert` dict.

    A re-sent payment has MORE live payouts than GL vouchers: the member was
    paid once in the books, but the bank was instructed twice. Legitimate repeat
    payments to the same person each carry their own GL voucher (serial), and
    failed legs move no money, so groups where every live leg is GL-backed are
    dropped. Returns the surviving alerts."""
    for df in (df_bank, df_gl):
        if not df.empty:
            df['transfer_alert'] = None
    if not alerts:
        return []
    gl_refs: Set[str] = set()
    if not df_gl.empty:
        for refs in df_gl['all_refs']:
            gl_refs.update(refs or [])
    kept = []
    for a in alerts:
        for leg in a['legs']:
            leg['gl_backed'] = bool((leg.get('serial') and leg['serial'] in gl_refs) or
                                    (leg.get('cms_ref') and leg['cms_ref'] in gl_refs))
        live = [l for l in a['legs'] if l['state'] != 'FAILED']
        if len(live) >= 1 and sum(l['gl_backed'] for l in live) < len(live) and len(a['legs']) >= 2:
            a['gl_vouchers'] = sum(l['gl_backed'] for l in a['legs'])
            kept.append(a)
    alerts[:] = kept
    if not alerts:
        return []
    gl_key = {}
    for a in alerts:
        for leg in a['legs']:
            for k in (leg.get('serial'), leg.get('cms_ref')):
                if k: gl_key[k] = a

    if not df_bank.empty:
        debits = df_bank[df_bank['type_sign'] == 'DEBIT']
        by_amt: Dict[float, List[Tuple[Any, str, list, Any]]] = {}
        for idx, narr, refs, amt, dt in zip(debits.index, debits['narration'], debits['all_refs'], debits['amount'], debits['dt']):
            by_amt.setdefault(round(amt, 2), []).append((idx, str(narr).upper(), refs or [], dt))
        for a in alerts:
            keys = {k for leg in a['legs'] for k in (leg.get('utr'), leg.get('cust_ref')) if k and len(k) >= 6}
            ifscs = {leg.get('ifsc') for leg in a['legs'] if leg.get('ifsc')}
            dts = [pd.to_datetime(l['date'], dayfirst=True) for l in a['legs']]
            lo, hi = min(dts) - timedelta(days=1), max(dts) + timedelta(days=10)
            confirmed, possible = [], []
            for idx, nu, refs, dt in by_amt.get(round(a['amount'], 2), []):
                if not (lo <= dt <= hi):
                    continue
                if any(k in refs or (not k.isdigit() and k in nu) for k in keys):
                    confirmed.append(idx)      # the bank row carries a leg's own reference
                elif any(i in nu for i in ifscs):
                    possible.append(idx)       # IMPS/NEFT to the same IFSC, RRN not in CMS yet
            if len(confirmed) >= 2 and len(confirmed) > a.get('gl_vouchers', 0):
                a['level'] = 'DOUBLE_DEBITED'
            # Same-IFSC debits are only surfaced while a leg is still in flight —
            # that is exactly when the stuck original may have gone through.
            if a['level'] not in ('RETRY_PENDING', 'DOUBLE_PAID'):
                possible = []
            a['possible_double_debit'] = len(confirmed) + len(possible) >= 2 and a['level'] != 'DOUBLE_DEBITED'
            a['bank_debits_seen'] = len(confirmed) + len(possible)
            summary = _alert_summary(a)
            for idx in confirmed + possible:
                df_bank.at[idx, 'transfer_alert'] = summary

    if not df_gl.empty:
        for idx, refs in zip(df_gl.index, df_gl['all_refs']):
            for r in refs or []:
                if r in gl_key:
                    df_gl.at[idx, 'transfer_alert'] = _alert_summary(gl_key[r])
                    break
    return alerts

def _alert_summary(a: Dict[str, Any]) -> Dict[str, Any]:
    return {'id': a['id'], 'level': a['level'], 'benef_name': a.get('benef_name'),
            'benef_acc': a['benef_acc'], 'amount': a['amount'],
            'possible_double_debit': bool(a.get('possible_double_debit')),
            'bank_debits_seen': a.get('bank_debits_seen', 0),
            'legs': [f"{l['date']} {l['mode']} {l['cms_ref'] or ''} [{l['state']}]".strip() for l in a['legs']]}

def check_statement_integrity(data_root: str) -> List[Dict[str, Any]]:
    """Validate each bank statement export via its running 'Available Balance':
    every row must equal previous balance +/- its amount. Gaps mean rows are
    missing from the export (e.g. a credits-only download) and that unmatched
    GL entries may simply have no bank row to match."""
    out = []
    for f in sorted(glob.glob(os.path.join(get_paths(data_root)['BANK_DIR'], "*.psv"))):
        try:
            df = pd.read_csv(f, sep='|', skiprows=7, dtype=str)
            df.columns = [c.strip() for c in df.columns]
            amt_col = next((c for c in df.columns if c.upper().startswith('TRANSACTION AMOUNT')), None)
            bal_col = next((c for c in df.columns if c.upper().startswith('AVAILABLE BALANCE')), None)
            if not amt_col or not bal_col or 'Cr/Dr' not in df.columns:
                continue
            sign = df['Cr/Dr'].str.strip().str.upper()
            amt = df[amt_col].map(parse_amount)
            bal = df[bal_col].map(parse_amount)
            signed = np.where(sign == 'DR', -amt, amt)
            diff = (bal - (bal.shift(1) + signed)).iloc[1:].round(2)
            gap_mask = diff.abs() > 0.01
            dates = df.get('Value Date', pd.Series([''] * len(df))).iloc[1:]
            n_dr, n_cr = int((sign == 'DR').sum()), int((sign == 'CR').sum())
            out.append({
                'file': os.path.basename(f), 'rows': len(df), 'debits': n_dr, 'credits': n_cr,
                'gaps': int(gap_mask.sum()),
                'unexplained_amount': round(float(diff[gap_mask].sum()), 2),
                'first_gap_date': str(dates[gap_mask].iloc[0]).strip() if gap_mask.any() else None,
                'one_sided': len(df) >= 20 and (n_dr == 0 or n_cr == 0),
                'ok': not gap_mask.any() and not (len(df) >= 20 and (n_dr == 0 or n_cr == 0)),
            })
        except Exception as e:
            logger.warning(f"Integrity check skipped for {f}: {e}")
    return out

def mark_settled_alerts(alerts: List[Dict[str, Any]], df_bank: pd.DataFrame, df_gl: pd.DataFrame) -> List[Dict[str, Any]]:
    """Flag re-send groups the user has already reconciled: every bank and GL
    row carrying the alert is matched, and nothing points to a second payout.
    Settled alerts stay in dq_stats (so they re-surface the moment the stuck
    original also debits — that turns on possible_double_debit) but the UI
    drops them from the alert banner."""
    rows: Dict[str, List[bool]] = {}
    for df in (df_bank, df_gl):
        if df.empty or 'transfer_alert' not in df.columns or 'matched' not in df.columns:
            continue
        for ta, m in zip(df['transfer_alert'], df['matched']):
            if isinstance(ta, dict) and ta.get('id'):
                rows.setdefault(ta['id'], []).append(bool(m))
    for a in alerts:
        flags = rows.get(a.get('id'), [])
        risky = a.get('level') in ('DOUBLE_DEBITED', 'DOUBLE_PAID') or a.get('possible_double_debit')
        a['settled'] = bool(flags) and all(flags) and not risky
    return alerts

_OUT_STATE = {'SUCCESS': 'PAID', 'CONFIRMED/SEND TO PROCESS': 'PENDING', 'FAILED/REVERSED': 'FAILED'}

def load_outward_status(data_root: str) -> List[Dict[str, Any]]:
    """Every row of the core-banking outward RTGS/NEFT/IMPS status report(s),
    including the ones without a CRN (rejected before reaching the bank)."""
    out = []
    for f in glob.glob(os.path.join(get_paths(data_root)['REPORTS_DIR'], "*.xml")):
        try:
            root = etree.parse(f).getroot()
            if 'OUTWARD' not in root.tag.upper():
                continue
            for n in root.xpath('.//G_CASH_ADJ'):
                st = (n.findtext('STATUS') or '').strip()
                serial = (n.findtext('SERIAL_NO') or '').strip()
                out.append({
                    'serial': serial.zfill(12) if serial else None,
                    'crn': (n.findtext('CRN_NUM') or '').strip() or None,
                    'date': (n.findtext('PAYMENT_DATE') or '').strip(),
                    'amount': parse_amount(n.findtext('PAY_AMOUNT')),
                    'benef_name': (n.findtext('BENEF_NAME') or '').strip().upper(),
                    'benef_acc': (n.findtext('BENEF_ACCNO') or '').strip(),
                    'ifsc': (n.findtext('BENEF_IFSC') or '').strip().upper(),
                    'method': (n.findtext('PAYMENT_METHOD') or '').strip().upper(),
                    'status': st,
                    'state': _OUT_STATE.get(st.upper(), 'UNKNOWN'),
                })
        except Exception as e:
            logger.warning(f"Outward status report skipped {f}: {e}")
    return out

def audit_outward_status(outward: List[Dict[str, Any]], cms: List[Dict[str, Any]], df_gl: pd.DataFrame) -> Dict[str, Any]:
    """Cross-check the core-banking software's outward status against the bank's
    CMS report (the source of truth for whether money actually moved).

    The software leaves IMPS rows at 'Confirmed/Send to Process' long after
    the bank has paid or rejected them. Verdicts:
      STALE_PAID     software pending, bank PAID       -> update status in software
      STALE_FAILED   software pending, bank CANCELLED  -> books show paid, money never left: post reversal
      IN_FLIGHT      software pending, bank no final status yet -> wait / check
      CONFLICT       software failed/reversed, bank PAID -> reversal posted on a payment that went out
      PAID_BUT_CANCELLED software success, bank CANCELLED -> member not paid
      NO_REVERSAL    software failed, no 'Failed' reversal voucher in the GL
    Rows that agree (both paid; failed with a GL reversal) are only counted."""
    by_serial: Dict[str, List[Dict]] = {}
    by_ref: Dict[str, List[Dict]] = {}
    for c in cms:
        if c.get('serial'): by_serial.setdefault(c['serial'], []).append(c)
        if c.get('cms_ref'): by_ref.setdefault(c['cms_ref'], []).append(c)
    reversed_serials: Set[str] = set()
    if not df_gl.empty:
        for narr in df_gl['narration']:
            s = str(narr).upper()
            if 'FAILED' in s:
                m = re.search(r'REF\s*NO\s*:?\s*(\d{12})', s)
                if m: reversed_serials.add(m.group(1))

    issues, counts = [], {'total': len(outward), 'agree': 0}
    for o in outward:
        hits = by_serial.get(o['serial'] or '') or by_ref.get((o['crn'] or '').split('/')[0]) or []
        states = {_payment_state(c) for c in hits}
        bank = 'PAID' if 'PAID' in states else 'FAILED' if states == {'FAILED'} else 'PENDING' if hits else 'NONE'
        sw = o['state']
        verdict = None
        if sw == 'PENDING':
            verdict = {'PAID': 'STALE_PAID', 'FAILED': 'STALE_FAILED'}.get(bank, 'IN_FLIGHT')
        elif sw == 'FAILED':
            if bank == 'PAID':
                verdict = 'CONFLICT'
            elif o['serial'] not in reversed_serials:
                verdict = 'NO_REVERSAL'
        elif sw == 'PAID' and bank == 'FAILED':
            verdict = 'PAID_BUT_CANCELLED'
        if not verdict:
            counts['agree'] += 1
            continue
        counts[verdict] = counts.get(verdict, 0) + 1
        c0 = next((c for c in hits if _payment_state(c) == bank), hits[0]) if hits else {}
        issues.append({**o, 'verdict': verdict, 'bank_state': bank,
                       'cms_status': c0.get('status') or None, 'cms_detail': c0.get('utr_raw'),
                       'utr': c0.get('utr'), 'cms_file': c0.get('source'),
                       'gl_reversed': o['serial'] in reversed_serials})
    order = {'CONFLICT': 0, 'PAID_BUT_CANCELLED': 1, 'STALE_FAILED': 2, 'NO_REVERSAL': 3, 'IN_FLIGHT': 4, 'STALE_PAID': 5}
    issues.sort(key=lambda x: (order.get(x['verdict'], 9), x['serial'] or ''))
    return {'counts': counts, 'issues': issues}

def load_cms_reports(data_root: str) -> List[Dict[str, Any]]:
    paths = get_paths(data_root)
    files = glob.glob(os.path.join(paths['CMS_DIR'], "*.xls*"))
    if not files: return []
    cms_data = []
    max_workers = min(4, len(files))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(_load_single_cms_report, files)
        for res in results:
            cms_data.extend(res)
    return cms_data

def _load_single_upi_collection_report(f: str) -> List[Dict[str, Any]]:
    records = []
    try:
        df = None
        # Try CSV / TSV first
        try:
            with open(f, 'r', encoding='utf-8', errors='ignore') as fp:
                probe_lines = [fp.readline() for _ in range(15)]
            header_row = 0
            for i, line in enumerate(probe_lines):
                line_u = line.upper()
                if any(k in line_u for k in ['BANK RRN', 'CUSTOMER VPA', 'PAYER NAME', 'RRN', 'TRANSACTION AMOUNT']):
                    header_row = i
                    break
            df = pd.read_csv(f, skiprows=header_row, encoding='utf-8', encoding_errors='ignore')
        except Exception:
            pass

        # Try Excel if needed
        if df is None or df.empty or len(df.columns) < 3:
            try:
                df_probe = pd.read_excel(f, nrows=15, header=None)
                header_row = 0
                for i, row in df_probe.iterrows():
                    row_u = [str(v).strip().upper() for v in row if pd.notna(v)]
                    if any('BANK RRN' in v or 'CUSTOMER VPA' in v or 'PAYER NAME' in v for v in row_u):
                        header_row = i
                        break
                df = pd.read_excel(f, skiprows=header_row)
            except Exception:
                pass

        if df is not None and not df.empty:
            df.columns = [str(c).strip() for c in df.columns]

            def find_col(keys):
                for k in keys:
                    for col in df.columns:
                        if k.upper() in col.upper():
                            return col
                return None

            rrn_col = find_col(['BANK RRN', 'RRN', 'BANK_RRN', 'UTR NO', 'UTR'])
            vpa_col = find_col(['CUSTOMER VPA', 'CUSTOMER_VPA', 'VPA', 'UPI ID', 'UPI_ID', 'CUSTOMER ID'])
            name_col = find_col(['PAYER NAME', 'PAYER_NAME', 'CUSTOMER NAME', 'SENDER NAME', 'BENEFICIARY NAME'])
            amt_col = find_col(['TRANSACTION AMOUNT', 'TXN AMOUNT', 'AMOUNT', 'TOTAL AMOUNT'])
            dt_col = find_col(['DATE OF TRANSACTION', 'TRANSACTION DATE', 'TXN DATE', 'DATE'])
            ref_col = find_col(['TRANSACTION REFERENCE ID', 'TXN REFERENCE ID', 'TRANSACTION ID', 'REF NO'])
            status_col = find_col(['TRANSACTION STATUS', 'TXN STATUS', 'STATUS'])

            for _, r in df.iterrows():
                try:
                    if status_col and pd.notna(r[status_col]):
                        st = str(r[status_col]).strip().upper()
                        if any(f_word in st for f_word in ['FAIL', 'DECLINE', 'REJECT', 'BOUNCE']):
                            continue

                    rrn_raw = str(r[rrn_col]).strip().replace('.0', '') if rrn_col and pd.notna(r[rrn_col]) else None
                    if not rrn_raw or rrn_raw.lower() in ('nan', 'none', ''):
                        continue
                    rrn_clean = re.sub(r'\D', '', rrn_raw)
                    if not rrn_clean:
                        continue
                    rrn = rrn_clean.zfill(12)

                    vpa_raw = str(r[vpa_col]).strip() if vpa_col and pd.notna(r[vpa_col]) else None
                    vpa = vpa_raw if vpa_raw and vpa_raw.lower() not in ('nan', 'none', '') else None

                    name_raw = str(r[name_col]).strip() if name_col and pd.notna(r[name_col]) else None
                    name = name_raw if name_raw and name_raw.lower() not in ('nan', 'none', '') else None

                    amt_raw = r[amt_col] if amt_col and pd.notna(r[amt_col]) else None
                    amt = parse_amount(amt_raw) if amt_raw is not None else None

                    gw_raw = str(r[ref_col]).strip() if ref_col and pd.notna(r[ref_col]) else None
                    gw = gw_raw if gw_raw and gw_raw.lower() not in ('nan', 'none', '') else None

                    records.append({
                        'rrn': rrn,
                        'vpa': vpa,
                        'payer_name': name,
                        'amount': amt,
                        'gateway_ref': gw
                    })
                except Exception:
                    continue
    except Exception as e:
        logger.error(f"Error loading UPI Collection Report {f}: {e}")
    return records

def load_upi_collection_reports(data_root: str) -> List[Dict[str, Any]]:
    paths = get_paths(data_root)
    upi_dir = paths.get('UPI_DIR')
    if not upi_dir or not os.path.exists(upi_dir):
        return []
    files = glob.glob(os.path.join(upi_dir, "*.csv")) + \
            glob.glob(os.path.join(upi_dir, "*.tsv")) + \
            glob.glob(os.path.join(upi_dir, "*.xls*"))
    if not files:
        return []
    records = []
    max_workers = min(4, len(files))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(_load_single_upi_collection_report, files)
        for res in results:
            records.extend(res)
    return records

def enrich_bank_with_upi_reports(df_bank: pd.DataFrame, upi_reports: List[Dict[str, Any]]) -> pd.DataFrame:
    if df_bank.empty:
        df_bank['enriched_by_upi_report'] = False
        return df_bank
    if 'enriched_by_upi_report' not in df_bank.columns:
        df_bank['enriched_by_upi_report'] = False
    if not upi_reports:
        return df_bank

    by_rrn = {}
    by_gw = {}
    for r in upi_reports:
        rrn = r.get('rrn')
        if rrn:
            by_rrn[rrn] = r
        gw = r.get('gateway_ref')
        if gw and len(gw) >= 10:
            by_gw[gw.upper()] = r

    enriched_count = 0
    for idx, row in df_bank.iterrows():
        matched_rec = None
        ref = row.get('ref_id')
        if ref and ref in by_rrn:
            matched_rec = by_rrn[ref]
        elif row.get('all_refs'):
            for r_cand in row['all_refs']:
                if r_cand in by_rrn:
                    matched_rec = by_rrn[r_cand]
                    break

        if not matched_rec and by_gw:
            narr_upper = str(row.get('narration', '')).upper()
            for gw_key, r in by_gw.items():
                if gw_key in narr_upper:
                    matched_rec = r
                    break

        if matched_rec:
            enriched_count += 1
            df_bank.at[idx, 'enriched_by_upi_report'] = True
            vpa = matched_rec.get('vpa')
            p_name = matched_rec.get('payer_name')
            if vpa:
                df_bank.at[idx, 'upi_id'] = vpa.strip().upper()
            if p_name:
                df_bank.at[idx, 'party_name'] = p_name.strip().upper()

    logger.info(f"Enriched {enriched_count} Bank entries with UPI collection report details")
    return df_bank

def enrich_gl_with_upi_reports(df_gl: pd.DataFrame, upi_reports: List[Dict[str, Any]]) -> pd.DataFrame:
    if df_gl.empty:
        df_gl['enriched_by_upi_report'] = False
        return df_gl
    if 'enriched_by_upi_report' not in df_gl.columns:
        df_gl['enriched_by_upi_report'] = False
    if not upi_reports:
        return df_gl

    by_rrn = {r['rrn']: r for r in upi_reports if r.get('rrn')}

    enriched_count = 0
    for idx, row in df_gl.iterrows():
        matched_rec = None
        ref = row.get('ref_id')
        if ref and ref in by_rrn:
            matched_rec = by_rrn[ref]
        elif row.get('all_refs'):
            for r_cand in row['all_refs']:
                if r_cand in by_rrn:
                    matched_rec = by_rrn[r_cand]
                    break

        if matched_rec:
            enriched_count += 1
            df_gl.at[idx, 'enriched_by_upi_report'] = True
            vpa = matched_rec.get('vpa')
            p_name = matched_rec.get('payer_name')
            if vpa and (pd.isna(row.get('upi_id')) or 'XX' in str(row.get('upi_id', ''))):
                df_gl.at[idx, 'upi_id'] = vpa.strip().upper()
            if p_name and pd.isna(row.get('party_name')):
                df_gl.at[idx, 'party_name'] = p_name.strip().upper()

    logger.info(f"Enriched {enriched_count} GL entries with UPI collection report details")
    return df_gl

def enrich_gl_with_reports(df_gl: pd.DataFrame, rtgs: List[Dict], cms: List[Dict]) -> pd.DataFrame:
    if df_gl.empty: return df_gl
    logger.info(f"Enriching GL with {len(rtgs)} RTGS and {len(cms)} CMS reports")
    df_gl['enriched_by_bridge'] = False
    
    # 1. Build O(1) hash lookups for CMS bridge
    bridge = {}
    for c in cms:
        refs = [r for r in [c['utr'], c['cust_ref']] if r]
        for key in [c['serial'], c['cms_ref']]:
            if key:
                bridge.setdefault(key, []).extend([r for r in refs if r not in bridge.get(key, [])])
    
    # 2. Build O(1) hash index for RTGS by serial and by inward account key
    rtgs_by_serial = {}
    rtgs_by_acc = {}
    for r in rtgs:
        s = r.get('serial')
        if s:
            rtgs_by_serial.setdefault(s, []).append(r)
        ak = r.get('acc_key')
        if ak:
            rtgs_by_acc.setdefault(ak, []).append(r)
            
    enriched_count = 0
    for idx, row in df_gl.iterrows():
        refs = list(row.get('all_refs', []))
        serial_extra = extract_serial(row['narration'])
        if serial_extra and serial_extra not in refs:
            refs.append(serial_extra)
            
        found_something = False
        for ref in list(refs):
            if ref in bridge:
                new_refs = [r for r in bridge[ref] if r not in refs]
                if new_refs:
                    refs.extend(new_refs)
                    found_something = True
            
            # O(1) lookup in RTGS serial index
            rtgs_m = rtgs_by_serial.get(ref, [])
            if rtgs_m:
                crn = str(rtgs_m[0]['crn']).upper()
                crn_clean = re.split(r'[/_]', crn)[0]
                if crn_clean in bridge:
                    new_refs = [r for r in bridge[crn_clean] if r not in refs]
                    if new_refs:
                        refs.extend(new_refs)
                        found_something = True
                elif crn not in refs:
                    refs.append(crn)
                    found_something = True

            # Inward RTGS bridge via account key (e.g. 1970-3804)
            if ref in rtgs_by_acc:
                for in_r in rtgs_by_acc[ref]:
                    in_crn = str(in_r.get('crn', '')).upper()
                    if in_crn and in_crn not in refs:
                        refs.append(in_crn)
                        found_something = True
        
        if found_something:
            enriched_count += 1
            df_gl.at[idx, 'all_refs'] = refs
            df_gl.at[idx, 'enriched_by_bridge'] = True
            def score(r):
                s = str(r); l, a = len(s), any(c.isalpha() for c in s)
                if a and l >= 12: return 100+l
                if not a and l == 12: return 90
                return l
            df_gl.at[idx, 'ref_id'] = sorted(refs, key=score, reverse=True)[0]
    
    logger.info(f"Enriched {enriched_count} GL entries with additional references")
    return df_gl

def _load_single_gl_xml(f: str) -> List[Dict[str, Any]]:
    rows = []
    try:
        root = etree.parse(f).getroot()
        for day in root.xpath('//G_DAY_DT'):
            try: dt = pd.to_datetime(day.findtext('DAY_DT'), dayfirst=True)
            except: continue
            for p in day.xpath('.//G_PARTI'):
                narr = normalize_narration(p.findtext('PARTI'))
                c, d = parse_amount(p.findtext('CREDIT')), parse_amount(p.findtext('DEBIT'))
                amt = d if d > 0 else c
                if amt == 0: continue
                tr = re.search(r'Transfer To (\d+)/(\d+)/', narr, re.I)
                refs = list(set(extract_refs(narr) + ([f"{tr.group(2)}-{tr.group(1)}"] if tr else [])))
                rows.append({
                    'date': dt.strftime('%d-%m-%Y'), 'dt': dt, 'narration': narr,
                    'amount': round(amt, 2), 'type_sign': 'DEBIT' if d > 0 else 'CREDIT',
                    'txn_type': detect_type(narr), 'ref_id': refs[0] if refs else None,
                    'all_refs': refs, 'cheque_no': extract_cheque(narr)
                })
    except Exception as e: logger.error(f"Error loading GL {f}: {e}")
    return rows

def load_raw_gl_xml(data_root: str) -> pd.DataFrame:
    paths = get_paths(data_root)
    empty_cols = ['gl_id', 'date', 'dt', 'narration', 'amount', 'type_sign', 'txn_type', 'ref_id', 'all_refs', 'cheque_no', 'original_refs']
    files = glob.glob(os.path.join(paths['GL_DIR'], "*.xml"))
    if not files: return pd.DataFrame(columns=empty_cols)
    rows = []
    max_workers = min(4, len(files))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(_load_single_gl_xml, files)
        for res in results:
            rows.extend(res)
            
    if not rows: return pd.DataFrame(columns=empty_cols)
    df_gl = pd.DataFrame(rows)
    df_gl['gl_id'] = df_gl.apply(lambda r: hashlib.md5(f"{r['date']}|{r['narration']}|{r['amount']}|{r['type_sign']}".encode()).hexdigest()[:12], axis=1)
    gl_id_counts = {}
    def make_unique(row):
        base = row['gl_id']
        count = gl_id_counts.get(base, 0)
        gl_id_counts[base] = count + 1
        return base if count == 0 else f"{base}_{count+1}"
    df_gl['gl_id'] = df_gl.apply(make_unique, axis=1)
    df_gl['original_refs'] = df_gl['all_refs'].apply(list)
    return df_gl

def load_gl_xml(data_root: str, rtgs: List[Dict], cms: List[Dict]) -> pd.DataFrame:
    df_gl_raw = load_raw_gl_xml(data_root)
    return enrich_gl_with_reports(df_gl_raw, rtgs, cms)

# Per-data-root results of load_all that are not row data (see load_all).
_SIDECAR: Dict[str, Dict[str, Any]] = {}

def _side_key(data_root: str) -> str:
    return os.path.normcase(os.path.abspath(data_root))

def _sidecar(data_root: str) -> Dict[str, Any]:
    return _SIDECAR.get(_side_key(data_root), {})

def load_all(data_root: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """The one ingestion pipeline: load every source in parallel, bridge GL refs
    via RTGS/CMS, enrich both sides from InstaBIZ UPI reports, and tag
    double-transfer groups. Returns (df_bank, df_gl) ready for reconcile/quick_save."""
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_rtgs = ex.submit(load_rtgs_reports, data_root)
        f_cms = ex.submit(load_cms_reports, data_root)
        f_upi = ex.submit(load_upi_collection_reports, data_root)
        f_bank = ex.submit(load_bank_psv, data_root)
        f_gl = ex.submit(load_raw_gl_xml, data_root)
        f_out = ex.submit(load_outward_status, data_root)
        rtgs, cms, upi_reports = f_rtgs.result(), f_cms.result(), f_upi.result()
        df_bank, df_gl_raw = f_bank.result(), f_gl.result()
        outward = f_out.result()
    df_gl = enrich_gl_with_reports(df_gl_raw, rtgs, cms)
    df_bank = enrich_bank_with_upi_reports(df_bank, upi_reports)
    df_gl = enrich_gl_with_upi_reports(df_gl, upi_reports)
    alerts = apply_transfer_alerts(df_bank, df_gl, detect_transfer_alerts(cms))
    # Kept in a side store, NOT df.attrs: pandas deep-copies attrs into every
    # derived object (each iterrows row, every drop), which made reconcile ~6x
    # slower. Alerts whose legs hit no loaded row still reach the dashboard.
    _SIDECAR[_side_key(data_root)] = {
        'transfer_alerts': [_alert_summary(a) for a in alerts],
        'outward_status': audit_outward_status(outward, cms, df_gl) if outward else None,
    }
    flag_copied_narrations(df_gl)
    if alerts:
        logger.warning(f"{len(alerts)} possible double-transfer group(s) flagged: "
                       + ", ".join(f"{a['benef_name']} {a['amount']} [{a['level']}]" for a in alerts[:5]))
    return df_bank, df_gl

_FAIL_WORDS_RX = re.compile(r'\b(?:FAILED|RETURN(?:ED)?|REVERS(?:AL|ED)|BOUNCE)\b', re.I)

def flag_copied_narrations(df_gl: pd.DataFrame) -> None:
    """The GL is keyed by hand, and copy-paste leaves an OLD entry's narration
    (and its UTR) on a new voucher. A 12-digit UTR is unique to one payment, so
    when two GL entries for different amounts/dates carry the same one, every
    later entry is flagged `copied_narration` (pointing at the original). Its
    narration cannot be trusted for matching - the amount/date/contra account
    must decide. Failed/returned twins legitimately share the serial and are
    ignored; same-amount same-date repeats are already DUPn duplicates."""
    if df_gl.empty:
        return
    df_gl['copied_narration'] = None
    first_by_utr: Dict[str, Tuple[Any, str, float, str]] = {}
    for idx, narr, dt, amt, date in zip(df_gl.index, df_gl['narration'], df_gl['dt'], df_gl['amount'], df_gl['date']):
        if _FAIL_WORDS_RX.search(str(narr)):
            continue
        u = primary_txn_ref(narr)
        if u:
            prev = first_by_utr.get(u)
            if prev is None:
                first_by_utr[u] = (idx, date, amt, narr)
                continue
            _, p_date, p_amt, _ = prev
            if p_date == date:
                continue  # same-day: an exact repeat (DUPn tracker) or a legitimate split posting
            df_gl.at[idx, 'copied_narration'] = {'utr': u, 'original_date': p_date, 'original_amount': p_amt,
                                                 'same_amount': abs(p_amt - amt) < 0.005}

def primary_txn_ref(narr: str) -> Optional[str]:
    """The narration's own transaction reference (not an account number, phone
    or VPA digits): UPI UTR, IMPS RRN, or the 'Ref No:' serial of RTGS/NEFT lines."""
    n = str(narr or '')
    m = re.search(r'Ref\s*No\s*[:=]?\s*(\d{10,12})\b', n, re.I)
    if m:
        return m.group(1).zfill(12)
    m = re.search(r'\bMMT/IMPS/(\d{12})\b', n, re.I)
    if m:
        return m.group(1)
    if re.search(r'U?PI/', n, re.I):
        u = parse_upi_narration(n).get('utr')
        if u:
            return u
        m = re.search(r'U?PI/(\d{12})(?!\d)', n, re.I)
        return m.group(1) if m else None
    return None

_CORRECTION_RX = re.compile(
    r'(?:\bcorrect(?:ed)?(?:\s+narration)?|missing\s+narration|actual(?:\s+narration)?|should\s+be|original\s+narration)'
    r'\s*(?:is|=|:|-)?\s*(.+)', re.I | re.S)

def apply_note_corrections(df: pd.DataFrame, id_col: str, notes: Dict[str, Any]) -> int:
    """Use the user's own sticky-note corrections as matching evidence. Notes such
    as 'Wrong narration. Correct = UPI/955448034498/...' carry the true reference;
    its UTR-like refs are appended to `all_refs` and the text kept as
    `corrected_narration` (the displayed narration is never altered)."""
    if df.empty or not notes:
        return 0
    if 'corrected_narration' not in df.columns:
        df['corrected_narration'] = None
    n = 0
    id_to_idx = dict(zip(df[id_col], df.index))
    for tx_id, note in notes.items():
        idx = id_to_idx.get(tx_id)
        if idx is None or not isinstance(note, dict):
            continue
        m = _CORRECTION_RX.search(str(note.get('text', '')))
        if not m:
            continue
        corrected = m.group(1).strip()
        new_refs = [r for r in extract_refs(corrected) if any(ch.isdigit() for ch in r) and len(r) >= 9]
        if not corrected:
            continue
        df.at[idx, 'corrected_narration'] = corrected[:300]
        if new_refs:
            refs = list(df.at[idx, 'all_refs'] or [])
            df.at[idx, 'all_refs'] = refs + [r for r in new_refs if r not in refs]
            n += 1
    return n

def collect_transfer_alerts(df_bank: pd.DataFrame, df_gl: pd.DataFrame, data_root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Unique alert summaries from load_all's side store plus either frame's
    `transfer_alert` column."""
    side = _sidecar(data_root).get('transfer_alerts', []) if data_root else []
    out = {a['id']: dict(a) for a in side if isinstance(a, dict)}
    for df in (df_gl, df_bank):
        if not df.empty and 'transfer_alert' in df.columns:
            for a in df['transfer_alert']:
                if isinstance(a, dict):
                    out[a["id"]] = dict(a)  # copy: mark_settled_alerts writes to it
    order = {'DOUBLE_DEBITED': 0, 'DOUBLE_PAID': 1, 'RETRY_PENDING': 2, 'RETRY_OK': 3}
    return sorted(out.values(), key=lambda a: order.get(a['level'], 9))


# --- ENGINE ---
def reconcile(df_bank: Optional[pd.DataFrame] = None, df_gl: Optional[pd.DataFrame] = None, data_root: str = DEFAULT_DATA_ROOT, progress_cb: Optional[Callable] = None, global_upi_registry: Optional[Set[str]] = None) -> Dict:
    def report(p, m): 
        if progress_cb: progress_cb(p, m)
    t_start = time.time()
    paths = get_paths(data_root)
    report(5, "Loading files in parallel...")

    # Frames handed in by the app cache are already fully enriched by load_all();
    # re-running the UPI/bridge enrichment on them was pure duplicate work.
    if df_bank is None or df_gl is None:
        loaded_bank, loaded_gl = load_all(data_root)
        df_bank = loaded_bank if df_bank is None else df_bank
        df_gl = loaded_gl if df_gl is None else df_gl
    for df in (df_bank, df_gl):
        if 'transfer_alert' not in df.columns:
            df['transfer_alert'] = None

    t_loaded = time.time()
    logger.info(f"Startup ingestion completed in {t_loaded - t_start:.3f} seconds.")
    if df_bank.empty and df_gl.empty: return {"error": "Empty data"}
    
    df_bank, df_gl = df_bank.copy(), df_gl.copy()
    if not df_bank.empty:
        df_bank = df_bank.sort_values(by='dt').reset_index(drop=True)
    if not df_gl.empty:
        df_gl = df_gl.sort_values(by='dt').reset_index(drop=True)
    df_bank['matched'] = False
    df_gl['matched'] = False
    df_bank = compute_duplicates(df_bank, global_upi_registry)
    df_gl = compute_duplicates(df_gl, global_upi_registry)
    if 'copied_narration' not in df_gl.columns:
        flag_copied_narrations(df_gl)

    # The user's sticky-note corrections ('Wrong narration. Correct = UPI/<utr>/...')
    # become matching evidence for this run.
    try:
        notes = storage.load_json(os.path.join(data_root, 'sticky_notes.json'), {}) or {}
    except storage.CorruptJSONError as e:
        logger.error(f"Ignoring unreadable sticky notes for corrections: {e}")
        notes = {}
    n_corr = apply_note_corrections(df_gl, 'gl_id', notes) + apply_note_corrections(df_bank, 'bank_id', notes)
    if n_corr:
        logger.info(f"Applied {n_corr} narration corrections from sticky notes")

    matches = []
    try:
        aliases = storage.load_json(paths['ALIASES_PATH'], {}) or {}
    except storage.CorruptJSONError as e:
        logger.error(f"Ignoring unreadable alias file: {e}")
        aliases = {}

    # 1. Manual & Orphan Pruning
    report(20, "Pruning Manual Links...")
    if os.path.exists(paths['MANUAL_LINKS_PATH']):
        try:
            manual = storage.load_json(paths['MANUAL_LINKS_PATH'], []) or []
            clean_manual, orphans = [], []
            for m in manual:
                b_ids = list(m.get('bank_ids', []))
                g_ids = list(m.get('gl_ids', []))
                
                # Expand base bank_ids to include their sequenced versions present in df_bank
                expanded_b_ids = []
                for bid in b_ids:
                    expanded_b_ids.append(bid)
                    if '_' not in bid:
                        base_rows = df_bank[df_bank['bank_id'] == bid]
                        if not base_rows.empty:
                            base_amt = base_rows.iloc[0]['amount']
                            matches_seq = df_bank[(df_bank['bank_id'].str.startswith(bid + '_')) & (df_bank['amount'] == base_amt)]['bank_id'].tolist()
                            expanded_b_ids.extend(matches_seq)
                b_ids_expanded = list(set(expanded_b_ids))
                
                b_active = df_bank['bank_id'].isin(b_ids_expanded) & ~df_bank['matched']
                g_active = df_gl['gl_id'].isin(g_ids) & ~df_gl['matched']
                
                if b_active.any() or g_active.any():
                    matches.append({
                        'status': 'MANUAL_MATCH', 
                        'bks': df_bank[b_active].drop(columns=['dt']).to_dict('records'), 
                        'gls': df_gl[g_active].drop(columns=['dt']).to_dict('records')
                    })
                    df_bank.loc[b_active, 'matched'] = True
                    df_gl.loc[g_active, 'matched'] = True
                    clean_manual.append(m)
                else:
                    # Only prune if both sides are truly missing from the dataframes
                    # (not just already matched)
                    if not df_bank['bank_id'].isin(b_ids_expanded).any() and not df_gl['gl_id'].isin(g_ids).any():
                        orphans.append(m)
                    else:
                        clean_manual.append(m)

            if orphans:
                # Never prune against a partial load: if either source frame is
                # empty, "missing from the dataframes" proves nothing.
                if df_bank.empty or df_gl.empty:
                    logger.warning(f"Skipped pruning {len(orphans)} apparent orphans: source data only partially loaded.")
                else:
                    logger.info(f"Pruned {len(orphans)} orphaned manual links.")
                    storage.save_json(paths['MANUAL_LINKS_PATH'], clean_manual)
        except Exception as e:
            logger.error(f"Error processing manual links: {e}")

    # 2. Bundled GST
    report(30, "GST Bundles...")
    
    # Group by the base bank_id (removing any _suffix) for GST Bundles
    if not df_bank.empty:
        df_bank['base_bank_id'] = df_bank['bank_id'].apply(lambda x: x.split('_')[0])
        gst_bank_groups = df_bank[~df_bank['matched']].groupby(['base_bank_id', 'date'])
    else:
        gst_bank_groups = []
        
    for (bid, bdt), group in gst_bank_groups:
        if len(group) >= 2 and group['narration'].str.contains('SGST|CGST|IPAY', case=False).any():
            gl_win = df_gl[~df_gl['matched'] & (df_gl['dt'] >= group.iloc[0]['dt'] - timedelta(days=15)) & (df_gl['dt'] <= group.iloc[0]['dt'] + timedelta(days=15))]
            tg, tb = [], []
            for bidx, brow in group.iterrows():
                m_gl = gl_win[(gl_win['amount'] == brow['amount']) & (~gl_win.index.isin(tg))]
                for _, mg in m_gl.iterrows():
                    if narrations_compatible(brow['narration'], mg['narration'], aliases):
                        tg.append(mg.name); tb.append(bidx); break
            if len(tb) >= 2:
                matches.append({'status': 'BUNDLED_GST_MATCH', 'bks': df_bank.loc[tb].drop(columns=['dt', 'base_bank_id']).to_dict('records'), 'gls': df_gl.loc[tg].drop(columns=['dt']).to_dict('records')})
                df_bank.loc[tb, 'matched'], df_gl.loc[tg, 'matched'] = True, True
                
    if 'base_bank_id' in df_bank.columns:
        df_bank = df_bank.drop(columns=['base_bank_id'])
    # 3. Optimized Tiers (Vectorized Windows via O(1) Dict Hashing)
    report(40, "Vectorized Matching...")
    t_match_start = time.time()
    
    bank_matched_status = df_bank['matched'].to_dict()
    gl_matched_status = df_gl['matched'].to_dict()

    # Pre-build unified amount index for Bank rows once to eliminate repeated full-table scans
    bank_by_amt = {}
    for bidx, brow in df_bank.iterrows():
        bank_by_amt.setdefault(brow['amount'], []).append((bidx, brow))
    bank_amts = sorted(bank_by_amt.keys())

    # TIER 1, TIER 2, TIER 2.5
    for status in ['TIER1_PERFECT', 'TIER2_UTR_MATCH', 'TIER2.5_CHEQUE_MATCH']:
        gl_rem = df_gl[~df_gl['matched']]
        if status != 'TIER2.5_CHEQUE_MATCH': gl_rem = gl_rem[gl_rem['ref_id'].notna()]
        else: gl_rem = gl_rem[gl_rem['cheque_no'].notna()]
        
        for idx, grow in gl_rem.iterrows():
            if gl_matched_status[idx]:
                continue
            target_amt = grow['amount']
            if target_amt not in bank_by_amt:
                continue
                
            cands = bank_by_amt[target_amt]
            for bidx, brow in cands:
                if bank_matched_status[bidx]:
                    continue
                if brow['type_sign'] == grow['type_sign']:
                    continue
                    
                days_apart = abs((brow['dt'] - grow['dt']).days)
                if status == 'TIER1_PERFECT' and brow['date'] != grow['date']:
                    continue
                elif status == 'TIER2_UTR_MATCH' and days_apart > 45:
                    continue
                elif status == 'TIER2.5_CHEQUE_MATCH':
                    if days_apart > 3 or brow.get('cheque_no') != grow.get('cheque_no'):
                        continue
                
                has_ref_overlap = bool(set(grow['all_refs']) & set(brow['all_refs']))
                is_near_match = False
                if not has_ref_overlap and status in ['TIER1_PERFECT', 'TIER2_UTR_MATCH']:
                    is_near_match = has_near_match_utr(grow['all_refs'], brow['all_refs'])

                if status == 'TIER2.5_CHEQUE_MATCH' or has_ref_overlap or is_near_match:
                    if status in ['TIER1_PERFECT', 'TIER2_UTR_MATCH']:
                        matched_refs = set(grow['all_refs']) & set(brow['all_refs'])
                        orig_refs = set(grow.get('original_refs', []))
                        is_transitive = bool(matched_refs and not (matched_refs & orig_refs))
                        
                        g_12d = {r for r in grow['all_refs'] if re.fullmatch(r'\d{12}', r)}
                        b_12d = {r for r in brow['all_refs'] if re.fullmatch(r'\d{12}', r)}
                        if g_12d and b_12d and not (g_12d & b_12d):
                            if not is_transitive and not is_near_match:
                                continue
                            
                    actual_status = status
                    if status == 'TIER1_PERFECT':
                        matched_refs = set(grow['all_refs']) & set(brow['all_refs'])
                        orig_refs = set(grow.get('original_refs', []))
                        if matched_refs and not (matched_refs & orig_refs):
                            actual_status = 'TIER1.1_TRANSITIVE_CMS'
                    
                    matches.append({'status': actual_status, 'bks': [brow.drop('dt').to_dict()], 'gls': [grow.drop('dt').to_dict()]})
                    df_bank.at[bidx, 'matched'] = True
                    df_gl.at[idx, 'matched'] = True
                    bank_matched_status[bidx] = True
                    gl_matched_status[idx] = True
                    break

    # TIER 2.1: FUZZY UTR (Single-Digit Typo / Dropped Digit on 11-13 Digit References, ±3 Days)
    report(42, "Tier 2.1: Fuzzy UTR Match...")
    gl_rem = df_gl[~df_gl['matched']]
    for idx, grow in gl_rem.iterrows():
        if gl_matched_status[idx]:
            continue
        target_amt = grow['amount']
        if target_amt not in bank_by_amt:
            continue
        g_cand_refs = [r for r in grow.get('all_refs', []) if 11 <= len(r) <= 13 and r.isdigit()]
        if not g_cand_refs:
            continue
        for bidx, brow in bank_by_amt[target_amt]:
            if bank_matched_status[bidx]:
                continue
            if brow['type_sign'] == grow['type_sign']:
                continue
            if abs((brow['dt'] - grow['dt']).days) > 3:
                continue
            b_12d = [r for r in brow.get('all_refs', []) if len(r) == 12 and r.isdigit()]
            matched_fuzzy = False
            for g_r in g_cand_refs:
                for b_r in b_12d:
                    if edit_distance(g_r, b_r) <= 1:
                        matched_fuzzy = True
                        break
                if matched_fuzzy:
                    break
            if matched_fuzzy:
                matches.append({
                    'status': 'TIER2.1_FUZZY_UTR',
                    'bks': [brow.drop('dt').to_dict()],
                    'gls': [grow.drop('dt').to_dict()],
                    'audit': {'confidence': 'HIGH', 'reason': 'Single-digit UTR typo resolved'}
                })
                df_bank.at[bidx, 'matched'] = True
                df_gl.at[idx, 'matched'] = True
                bank_matched_status[bidx] = True
                gl_matched_status[idx] = True
                break

    # TIER 2.6: INTER-BRANCH CLEARING AUTO-MATCHER
    report(45, "Tier 2.6: Inter-Branch Clearing...")
    gl_rem = df_gl[~df_gl['matched']]
    BRANCH_PATTERNS = r'\b(KOD|PKT|KTH)\b|KOD-|PKT-|KTH-'
    
    for idx, grow in gl_rem.iterrows():
        if gl_matched_status[idx]:
            continue
        target_amt = grow['amount']
        if target_amt not in bank_by_amt:
            continue
            
        g_m = re.search(BRANCH_PATTERNS, grow['narration'], re.I)
        if not g_m:
            continue
            
        g_br = g_m.group(1).upper() if g_m.group(1) else g_m.group(0).replace('-', '').upper()
        
        for bidx, brow in bank_by_amt[target_amt]:
            if bank_matched_status[bidx]:
                continue
            if brow['type_sign'] == grow['type_sign']:
                continue
            # Match within ±3-day window
            days_apart = abs((brow['dt'] - grow['dt']).days)
            if days_apart <= 3:
                if g_br in brow['narration'].upper() or narrations_compatible(grow['narration'], brow['narration'], aliases):
                    matches.append({
                        'status': 'TIER2.6_INTER_BRANCH_CLEARING',
                        'bks': [brow.drop('dt').to_dict()],
                        'gls': [grow.drop('dt').to_dict()],
                        'audit': {'confidence': 'HIGH', 'branch': g_br}
                    })
                    df_bank.at[bidx, 'matched'] = True
                    df_gl.at[idx, 'matched'] = True
                    bank_matched_status[bidx] = True
                    gl_matched_status[idx] = True
                    break

    # TIER 2.8: FISCAL YEAR-END MARCH 31 AUDIT CLEANUP
    # Catches entries posted on March 31 that clear earlier in the fiscal year with exact UTR
    report(48, "Tier 2.8: Year-End Audit Cleanup...")
    gl_rem = df_gl[~df_gl['matched']]
    for idx, grow in gl_rem.iterrows():
        if gl_matched_status[idx]:
            continue
        if not (grow['dt'].month == 3 and grow['dt'].day >= 28):
            continue
        target_amt = grow['amount']
        if target_amt not in bank_by_amt:
            continue
        g_refs = set(grow.get('all_refs', []))
        if not g_refs:
            continue
        for bidx, brow in bank_by_amt[target_amt]:
            if bank_matched_status[bidx]:
                continue
            if brow['type_sign'] == grow['type_sign']:
                continue
            if bool(g_refs & set(brow.get('all_refs', []))):
                matches.append({
                    'status': 'TIER2.8_YEAR_END_CLEANUP',
                    'bks': [brow.drop('dt').to_dict()],
                    'gls': [grow.drop('dt').to_dict()],
                    'audit': {'confidence': 'CRITICAL', 'reason': 'Year-End March 31 adjustment voucher'}
                })
                df_bank.at[bidx, 'matched'] = True
                df_gl.at[idx, 'matched'] = True
                bank_matched_status[bidx] = True
                gl_matched_status[idx] = True
                break

    def commit_pair(status, bidx, brow, idx, grow, audit):
        matches.append({'status': status, 'bks': [brow.drop('dt').to_dict()],
                        'gls': [grow.drop('dt').to_dict()], 'audit': audit})
        df_bank.at[bidx, 'matched'] = True
        df_gl.at[idx, 'matched'] = True
        bank_matched_status[bidx] = True
        gl_matched_status[idx] = True

    def utrs_of(refs):
        return {r for r in (refs or []) if len(r) == 12 and r.isdigit()}

    # TIER 2.2: LATE-POSTED UTR (exact 12-digit UTR + exact amount, any date gap)
    # Entries forgotten and posted months later still carry the same UTR. Only
    # UTRs that occur exactly once on each side qualify, so a duplicated or
    # re-used reference can never be auto-linked across a long gap.
    report(49, "Tier 2.2: Late-Posted UTR...")
    gl_utr_count, bank_utr_count = {}, {}
    for refs in df_gl['all_refs']:
        for u in utrs_of(refs): gl_utr_count[u] = gl_utr_count.get(u, 0) + 1
    bank_by_utr = {}
    for bidx, refs in zip(df_bank.index, df_bank['all_refs']):
        for u in utrs_of(refs):
            bank_utr_count[u] = bank_utr_count.get(u, 0) + 1
            bank_by_utr[u] = bidx
    for idx, grow in df_gl[~df_gl['matched']].iterrows():
        if gl_matched_status[idx]:
            continue
        for u in utrs_of(grow['all_refs']):
            if gl_utr_count.get(u) != 1 or bank_utr_count.get(u) != 1:
                continue
            bidx = bank_by_utr[u]
            if bank_matched_status[bidx]:
                continue
            brow = df_bank.loc[bidx]
            if brow['type_sign'] == grow['type_sign'] or abs(brow['amount'] - grow['amount']) > 0.005:
                continue
            days = abs((brow['dt'] - grow['dt']).days)
            commit_pair('TIER2.2_LATE_UTR_MATCH', bidx, brow, idx, grow,
                        {'confidence': 'HIGH', 'date_delay': days, 'utr': u,
                         'reason': f'Same unique UTR {u}, posted {days} days apart'})
            break

    # TIER 3: EXACT DATE (Optimized pure-Python Candidate Matching)
    report(50, "Tier 3: Exact Date...")
    gl_rem = df_gl[~df_gl['matched']]

    for idx, grow in gl_rem.iterrows():
        if gl_matched_status[idx]:
            continue
        target_amt = grow['amount']
        
        # O(log N) binary range queries using bisect for ±0.50 tolerance
        left_idx = bisect.bisect_left(bank_amts, target_amt - 0.50)
        right_idx = bisect.bisect_right(bank_amts, target_amt + 0.50)
        
        cands = []
        for a in bank_amts[left_idx:right_idx]:
            for bidx, brow in bank_by_amt[a]:
                if not bank_matched_status[bidx]:
                    cands.append((bidx, brow))
                    
        if not cands:
            continue
            
        cands_filtered = []
        for bidx, brow in cands:
            if brow['type_sign'] == grow['type_sign']:
                continue
            days_apart = abs((brow['dt'] - grow['dt']).days)
            if days_apart <= 1:
                amt_diff = abs(brow['amount'] - target_amt)
                cands_filtered.append((bidx, brow, days_apart, amt_diff))
                
        cands_filtered.sort(key=lambda x: (x[3], x[2]))
        
        for bidx, brow, days_apart, amt_diff in cands_filtered:
            if bank_matched_status[bidx]:
                continue
            if narrations_compatible(grow['narration'], brow['narration'], aliases) or \
               (amt_diff < 0.005 and utr_suffix_match(grow['narration'], brow['all_refs'])):
                matches.append({
                    'status': 'TIER3_EXACT_DATE',
                    'bks': [brow.drop('dt').to_dict()],
                    'gls': [grow.drop('dt').to_dict()],
                    'audit': {'confidence': 'HIGH', 'date_delay': days_apart, 'amount_diff': round(amt_diff, 2)}
                })
                df_bank.at[bidx, 'matched'] = True
                df_gl.at[idx, 'matched'] = True
                bank_matched_status[bidx] = True
                gl_matched_status[idx] = True
                break

    # TIER 3.2: CLEANED UPI VPA MATCH (±5 Days, Exact or Mask-Aware VPA handle)
    report(52, "Tier 3.2: UPI VPA Matching...")
    gl_rem = df_gl[~df_gl['matched']]
    for idx, grow in gl_rem.iterrows():
        if gl_matched_status[idx]:
            continue
        g_vpa = grow.get('upi_id') or extract_upi_id(grow['narration'])
        if not g_vpa:
            continue
        target_amt = grow['amount']
        if target_amt not in bank_by_amt:
            continue
        for bidx, brow in bank_by_amt[target_amt]:
            if bank_matched_status[bidx]:
                continue
            if brow['type_sign'] == grow['type_sign']:
                continue
            if abs((brow['dt'] - grow['dt']).days) <= 5:
                b_vpa = brow.get('upi_id') or extract_upi_id(brow['narration'])
                if b_vpa and vpa_matches(b_vpa, g_vpa):
                    matches.append({
                        'status': 'TIER3.2_UPI_VPA_MATCH',
                        'bks': [brow.drop('dt').to_dict()],
                        'gls': [grow.drop('dt').to_dict()],
                        'audit': {'confidence': 'HIGH', 'vpa': g_vpa}
                    })
                    df_bank.at[bidx, 'matched'] = True
                    df_gl.at[idx, 'matched'] = True
                    bank_matched_status[bidx] = True
                    gl_matched_status[idx] = True
                    break

    # TIER 3.3: INWARD RTGS/NEFT BY IFSC BANK (±1 day, exact amount)
    # Bank 'RTGS-SBINR1...' credit vs GL 'BY RTGS TRANSFER - SBI'. Only an
    # unambiguous single candidate is linked.
    report(53, "Tier 3.3: RTGS IFSC Bank Match...")
    for idx, grow in df_gl[~df_gl['matched']].iterrows():
        if gl_matched_status[idx] or grow['amount'] not in bank_by_amt:
            continue
        if not re.search(r'\b(?:RTGS|NEFT)\b', grow['narration'], re.I):
            continue
        cands = [(bidx, brow) for bidx, brow in bank_by_amt[grow['amount']]
                 if not bank_matched_status[bidx] and brow['type_sign'] != grow['type_sign']
                 and abs((brow['dt'] - grow['dt']).days) <= 1
                 and rtgs_ifsc_bank_matches(brow['narration'], grow['narration'])]
        if len(cands) == 1:
            bidx, brow = cands[0]
            commit_pair('TIER3.3_RTGS_IFSC_MATCH', bidx, brow, idx, grow,
                        {'confidence': 'HIGH', 'date_delay': abs((brow['dt'] - grow['dt']).days),
                         'reason': f"IFSC {brow['narration'][5:9].upper()} = bank named in GL"})

    # TIER 3.4: STATUTORY PAYMENTS (GST / PF / ESI / TDS via GIB, ≤31 days)
    # Bank remits through the GIB tax gateway mid-month; the GL books the same
    # liability at month-end. Same statutory class + exact amount, nearest date.
    report(54, "Tier 3.4: Statutory Payments...")
    for idx, grow in df_gl[~df_gl['matched']].iterrows():
        if gl_matched_status[idx] or grow['amount'] not in bank_by_amt:
            continue
        g_cls = statutory_class(grow['narration'])
        if not g_cls:
            continue
        cands = []
        for bidx, brow in bank_by_amt[grow['amount']]:
            if bank_matched_status[bidx] or brow['type_sign'] == grow['type_sign']:
                continue
            if not str(brow['narration']).upper().startswith('GIB/'):
                continue
            days = abs((brow['dt'] - grow['dt']).days)
            if days <= 31 and statutory_class(brow['narration']) == g_cls:
                cands.append((days, bidx, brow))
        if cands:
            days, bidx, brow = min(cands, key=lambda c: c[0])
            commit_pair('TIER3.4_STATUTORY_MATCH', bidx, brow, idx, grow,
                        {'confidence': 'HIGH', 'date_delay': days, 'class': g_cls})

    # TIER 3.5: ONACHANTHA SEASONAL STALL MATCHER
    report(55, "Tier 3.5: Onachantha Seasonal Stall Matcher...")
    gl_rem = df_gl[~df_gl['matched']]
    FESTIVAL_KEYWORDS = ['ONACHANTHA', 'ONASALES', 'PRAJIN', 'ONAM']
    
    for idx, grow in gl_rem.iterrows():
        if gl_matched_status[idx]:
            continue
        # Active only during Onam harvest season (August and September)
        if not (8 <= grow['dt'].month <= 9):
            continue
        target_amt = grow['amount']
        if target_amt not in bank_by_amt:
            continue
            
        is_festival_gl = any(kw in grow['narration'].upper() for kw in FESTIVAL_KEYWORDS)
        if not is_festival_gl:
            continue
            
        for bidx, brow in bank_by_amt[target_amt]:
            if bank_matched_status[bidx]:
                continue
            if brow['type_sign'] == grow['type_sign']:
                continue
            # Match within a ±7-day window
            days_apart = abs((brow['dt'] - grow['dt']).days)
            if days_apart <= 7:
                is_festival_bank = any(kw in brow['narration'].upper() for kw in FESTIVAL_KEYWORDS)
                if is_festival_bank or narrations_compatible(grow['narration'], brow['narration'], aliases):
                    matches.append({
                        'status': 'TIER3.5_ONACHANTHA_SALES_MATCH',
                        'bks': [brow.drop('dt').to_dict()],
                        'gls': [grow.drop('dt').to_dict()],
                        'audit': {'confidence': 'HIGH', 'festival': 'Onam'}
                    })
                    df_bank.at[bidx, 'matched'] = True
                    df_gl.at[idx, 'matched'] = True
                    bank_matched_status[bidx] = True
                    gl_matched_status[idx] = True
                    break

    # TIER 4: FUZZY WINDOW (Optimized pure-Python Candidate Matching)
    report(60, "Tier 4: Fuzzy Window...")
    gl_rem = df_gl[~df_gl['matched']]
    
    for idx, grow in gl_rem.iterrows():
        if gl_matched_status[idx]:
            continue
        target_amt = grow['amount']
        
        # O(log N) binary range queries using bisect for ±0.50 tolerance
        left_idx = bisect.bisect_left(bank_amts, target_amt - 0.50)
        right_idx = bisect.bisect_right(bank_amts, target_amt + 0.50)
        
        cands = []
        for a in bank_amts[left_idx:right_idx]:
            for bidx, brow in bank_by_amt[a]:
                if not bank_matched_status[bidx]:
                    cands.append((bidx, brow))
                    
        if not cands:
            continue
            
        cands_filtered = []
        for bidx, brow in cands:
            if brow['type_sign'] == grow['type_sign']:
                continue
            days_apart = abs((brow['dt'] - grow['dt']).days)
            if days_apart <= 5:
                amt_diff = abs(brow['amount'] - target_amt)
                cands_filtered.append((bidx, brow, days_apart, amt_diff))
                
        cands_filtered.sort(key=lambda x: (x[3], x[2]))
        
        for bidx, brow, days_apart, amt_diff in cands_filtered:
            if bank_matched_status[bidx]:
                continue
            if narrations_compatible(grow['narration'], brow['narration'], aliases):
                matches.append({
                    'status': 'TIER4_FUZZY_WINDOW',
                    'bks': [brow.drop('dt').to_dict()],
                    'gls': [grow.drop('dt').to_dict()],
                    'audit': {'confidence': 'MEDIUM', 'date_delay': days_apart, 'amount_diff': round(amt_diff, 2)}
                })
                df_bank.at[bidx, 'matched'] = True
                df_gl.at[idx, 'matched'] = True
                bank_matched_status[bidx] = True
                gl_matched_status[idx] = True
                break

    # TIER 4.5: MARCH-TO-MAY SUSPENSE BRIDGE RESOLVER
    report(65, "Tier 4.5: March-to-May Suspense Bridge...")
    gl_rem = df_gl[~df_gl['matched']]
    
    # 1. Extract potential suspense entries parked on March 31
    suspense_gl = []
    for idx, grow in gl_rem.iterrows():
        if grow['dt'].month == 3 and grow['dt'].day == 31:
            if any(kw in grow['narration'].upper() for kw in ['SL-758', 'SL 758', 'SUSPENSE']):
                suspense_gl.append((idx, grow))
                
    # 2. Extract potential reversal entries posted in late May (May 29 or 30)
    reversal_gl = {}
    for idx, grow in gl_rem.iterrows():
        if grow['dt'].month == 5 and grow['dt'].day in [29, 30]:
            amt = grow['amount']
            reversal_gl.setdefault(amt, []).append((idx, grow))
            
    for s_idx, s_row in suspense_gl:
        if gl_matched_status[s_idx]:
            continue
        amt = s_row['amount']
        
        # Seek late May reversal
        if amt not in reversal_gl:
            continue
            
        rev_row_cand = None
        rev_idx_cand = None
        for r_idx, r_row in reversal_gl[amt]:
            if gl_matched_status[r_idx]:
                continue
            if r_row['type_sign'] != s_row['type_sign']:
                rev_row_cand = r_row
                rev_idx_cand = r_idx
                break
                
        if rev_row_cand is None:
            continue
            
        # Find matching bank statements around late March/early April (up to 45 days prior to March 31)
        if amt not in bank_by_amt:
            continue
            
        for bidx, brow in bank_by_amt[amt]:
            if bank_matched_status[bidx]:
                continue
            if brow['type_sign'] != s_row['type_sign']: # Bank statement transaction matches opposite accounting side
                if brow['dt'] <= s_row['dt'] and brow['dt'] >= s_row['dt'] - timedelta(days=45):
                    if (narrations_compatible(brow['narration'], s_row['narration'], aliases) or 
                        narrations_compatible(brow['narration'], rev_row_cand['narration'], aliases)):
                        
                        matches.append({
                            'status': 'TIER4.5_MAR_MAY_SUSPENSE_BRIDGE',
                            'bks': [brow.drop('dt').to_dict()],
                            'gls': [s_row.drop('dt').to_dict(), rev_row_cand.drop('dt').to_dict()],
                            'audit': {'confidence': 'CRITICAL', 'suspense_acct': 'SL-758'}
                        })
                        df_bank.at[bidx, 'matched'] = True
                        df_gl.at[s_idx, 'matched'] = True
                        df_gl.at[rev_idx_cand, 'matched'] = True
                        bank_matched_status[bidx] = True
                        gl_matched_status[s_idx] = True
                        gl_matched_status[rev_idx_cand] = True
                        break

    # TIER 5: DEEP NARRATION (Optimized pure-Python Candidate Matching)
    report(70, "Tier 5: Deep Narration...")
    gl_rem = df_gl[~df_gl['matched']]
    
    for idx, grow in gl_rem.iterrows():
        if gl_matched_status[idx]:
            continue
        target_amt = grow['amount']
        if target_amt not in bank_by_amt:
            continue
            
        cands = bank_by_amt[target_amt]
        cands_filtered = []
        for bidx, brow in cands:
            if bank_matched_status[bidx]:
                continue
            if brow['type_sign'] == grow['type_sign']:
                continue
            days_apart = abs((brow['dt'] - grow['dt']).days)
            if days_apart <= 15:
                cands_filtered.append((bidx, brow, days_apart))
                
        cands_filtered.sort(key=lambda x: x[2]) # Sort by proximity
        
        for bidx, brow, days_apart in cands_filtered:
            if bank_matched_status[bidx]:
                continue
            res = narrations_compatible(grow['narration'], brow['narration'], aliases)
            if not res:
                # Wrap fuzzy SequenceMatcher override inside strict UTR conflict guard check!
                g_12d = {w for w in grow.get('all_refs', []) if re.fullmatch(r'\d{12}', w)}
                b_12d = {w for w in brow.get('all_refs', []) if re.fullmatch(r'\d{12}', w)}
                if not (g_12d and b_12d and not (g_12d & b_12d)):
                    res = difflib.SequenceMatcher(None, clean_narration(grow['narration']), clean_narration(brow['narration'])).ratio() > 0.72
            if res:
                matches.append({
                    'status': 'TIER5_DEEP_NARRATION',
                    'bks': [brow.drop('dt').to_dict()],
                    'gls': [grow.drop('dt').to_dict()],
                    'audit': {'confidence': 'LOW', 'date_delay': days_apart, 'amount_diff': 0.0}
                })
                df_bank.at[bidx, 'matched'] = True
                df_gl.at[idx, 'matched'] = True
                bank_matched_status[bidx] = True
                gl_matched_status[idx] = True
                break

    # TIER 5.5: COOPERATIVE SEMANTIC RECOVERY
    report(75, "Tier 5.5: Cooperative Semantic Recovery...")
    gl_rem = df_gl[~df_gl['matched']]
    MALAYALAM_TRIGGERS = {
        'RE_PLEDGE': r'GOLD\s+മാറ്റിവെ|മാറ്റിവെക്കുമ്പോൾ|RE-PLEDGE',
        'INSTALLMENT_FAIL': r'അടവ്\s+വന്നിട്ടില്ല|അടവ്|INSTALLMENT|MISSED'
    }
    
    for idx, grow in gl_rem.iterrows():
        if gl_matched_status[idx]:
            continue
        target_amt = grow['amount']
        if target_amt not in bank_by_amt:
            continue
            
        narr_upper = grow['narration'].upper()
        is_repledge = bool(re.search(MALAYALAM_TRIGGERS['RE_PLEDGE'], narr_upper, re.I))
        is_inst_fail = bool(re.search(MALAYALAM_TRIGGERS['INSTALLMENT_FAIL'], narr_upper, re.I))
        
        if not (is_repledge or is_inst_fail):
            continue
            
        for bidx, brow in bank_by_amt[target_amt]:
            if bank_matched_status[bidx]:
                continue
            if brow['type_sign'] == grow['type_sign']:
                continue
            # Match within a ±15-day window
            days_apart = abs((brow['dt'] - grow['dt']).days)
            if days_apart <= 15:
                matches.append({
                    'status': 'TIER5.5_COOP_SEMANTIC_RECOVERY',
                    'bks': [brow.drop('dt').to_dict()],
                    'gls': [grow.drop('dt').to_dict()],
                    'audit': {
                        'confidence': 'MEDIUM',
                        'type': 'RE-PLEDGE' if is_repledge else 'INSTALLMENT_FAILURE'
                    }
                })
                df_bank.at[bidx, 'matched'] = True
                df_gl.at[idx, 'matched'] = True
                bank_matched_status[bidx] = True
                gl_matched_status[idx] = True
                break

    # 5. Tier 6: Automated One-to-Many Grouping
    report(80, "Tier 6: One-to-Many Grouping...")
    gl_groups = df_gl[~df_gl['matched']].groupby(['date', 'narration', 'type_sign'])
    for (g_date, g_narr, g_sign), group in gl_groups:
        if len(group) > 1:
            total_amt = round(group['amount'].sum(), 2)
            opp_sign = 'CREDIT' if g_sign == 'DEBIT' else 'DEBIT'
            if total_amt in bank_by_amt:
                for bidx, brow in bank_by_amt[total_amt]:
                    if bank_matched_status[bidx]:
                        continue
                    if brow['type_sign'] == opp_sign and abs((brow['dt'] - group.iloc[0]['dt']).days) <= 3:
                        if narrations_compatible(g_narr, brow['narration'], aliases):
                            matches.append({
                                'status': 'TIER6_ONE_TO_MANY', 
                                'bks': [brow.drop('dt').to_dict()], 
                                'gls': group.drop(columns=['dt']).to_dict('records')
                            })
                            df_bank.at[bidx, 'matched'] = True
                            df_gl.loc[group.index, 'matched'] = True
                            bank_matched_status[bidx] = True
                            for g_idx in group.index:
                                gl_matched_status[g_idx] = True
                            break

    # 5b. Tier 6b: Automated Many-to-One Bank Grouping
    report(85, "Tier 6b: Many-to-One Bank Grouping...")
    bank_groups = df_bank[~df_bank['matched']].groupby(['date', 'narration', 'type_sign'])
    for (b_date, b_narr, b_sign), group in bank_groups:
        if len(group) > 1:
            total_amt = round(group['amount'].sum(), 2)
            opp_sign = 'CREDIT' if b_sign == 'DEBIT' else 'DEBIT'
            m = (~df_gl['matched']) & ((df_gl['amount'] - total_amt).abs() < 0.005) & (df_gl['type_sign'] == opp_sign)
            m &= (df_gl['dt'] - group.iloc[0]['dt']).dt.days.abs() <= 3
            cands = df_gl[m]
            if not cands.empty:
                g_row = cands.iloc[0]
                if narrations_compatible(b_narr, g_row['narration'], aliases):
                    matches.append({'status': 'TIER6B_MANY_TO_ONE', 'bks': group.drop(columns=['dt']).to_dict('records'), 'gls': [g_row.drop('dt').to_dict()]})
                    df_bank.loc[group.index, 'matched'] = True
                    df_gl.at[g_row.name, 'matched'] = True
                    for b_idx in group.index:
                        bank_matched_status[b_idx] = True
                    gl_matched_status[g_row.name] = True

    # 6. Ultra-Fast Indexed Internal Offsets (GL and Bank Reversals / Returns)
    report(90, "Internal Offsets...")
    for df, side in [(df_gl, 'GL'), (df_bank, 'BANK')]:
        unmatched_map = {}
        for idx, row in df.iterrows():
            is_m = bank_matched_status[idx] if side == 'BANK' else gl_matched_status[idx]
            if is_m:
                continue
            unmatched_map.setdefault(row['amount'], []).append((idx, row))
            
        for amt, row_list in unmatched_map.items():
            debits = [r for r in row_list if r[1]['type_sign'] == 'DEBIT']
            credits = [r for r in row_list if r[1]['type_sign'] == 'CREDIT']
            for d_idx, d_row in debits:
                is_m_d = bank_matched_status[d_idx] if side == 'BANK' else gl_matched_status[d_idx]
                if is_m_d:
                    continue
                for c_idx, c_row in credits:
                    is_m_c = bank_matched_status[c_idx] if side == 'BANK' else gl_matched_status[c_idx]
                    if is_m_c:
                        continue
                    if abs((c_row['dt'] - d_row['dt']).days) <= 45:
                        # Normalize reversal status words (e.g. 'RTGS Failed To Saujath' vs 'RTGS To Saujath')
                        n1 = re.sub(r'\b(FAILED|REVERSAL|BOUNCE|RETURNED|REVERSED)\b', '', d_row['narration'], flags=re.I)
                        n2 = re.sub(r'\b(FAILED|REVERSAL|BOUNCE|RETURNED|REVERSED)\b', '', c_row['narration'], flags=re.I)
                        n1_clean = clean_narration(n1)
                        n2_clean = clean_narration(n2)
                        if (n1_clean == n2_clean and n1_clean != "") or \
                           (len(n1_clean) >= 8 and (n1_clean in n2_clean or n2_clean in n1_clean)) or \
                           narrations_compatible(d_row['narration'], c_row['narration'], aliases):
                            matches.append({
                                'status': f'INTERNAL_OFFSET_{side}',
                                'bks': [d_row.drop('dt').to_dict(), c_row.drop('dt').to_dict()] if side == 'BANK' else [],
                                'gls': [d_row.drop('dt').to_dict(), c_row.drop('dt').to_dict()] if side == 'GL' else []
                            })
                            df.at[d_idx, 'matched'] = True
                            df.at[c_idx, 'matched'] = True
                            if side == 'BANK':
                                bank_matched_status[d_idx] = True
                                bank_matched_status[c_idx] = True
                            else:
                                gl_matched_status[d_idx] = True
                                gl_matched_status[c_idx] = True
                            break

    t_match_end = time.time()
    logger.info(f"Reconciliation matching completed in {t_match_end - t_match_start:.3f} seconds.")
    report(95, "Finalizing...")
    # Duplicate/UPI structure was computed at load; only the matched flag moved
    df_bank = refresh_duplicate_matched(df_bank)
    df_gl = refresh_duplicate_matched(df_gl)
    un_b, un_g = df_bank[~df_bank['matched']], df_gl[~df_gl['matched']]
    # Unmatched bank rows by UTR, to surface "same UTR, different amount" (a GL
    # posting error) which the exact-amount candidate scan below can never see.
    un_b_by_utr = {}
    for _, brow in un_b.iterrows():
        for u in utrs_of(brow['all_refs']):
            un_b_by_utr.setdefault(u, []).append(brow)
    missing_bank = []
    for _, grow in un_g.iterrows():
        rec = grow.drop('dt').to_dict()
        if grow['is_duplicate_matched']:
            rec['duplicate_info'] = f"Duplicate {grow['duplicate_index']}: Ref {grow['duplicate_ref']} (PARTNER LINKED)"
        elif grow['duplicate_index'] is not None:
            rec['duplicate_info'] = f"Duplicate {grow['duplicate_index']}: Ref {grow['duplicate_ref']}"
            
        m = (un_b['amount'] == grow['amount']) & (un_b['type_sign'] != grow['type_sign'])
        cands = un_b[m].copy()
        if not cands.empty:
            results = []
            for _, brow in cands.iterrows():
                days = abs((brow['dt'] - grow['dt']).days)
                narr_sim = round(difflib.SequenceMatcher(None, clean_narration(grow['narration']), clean_narration(brow['narration'])).ratio() * 100)
                
                # Call advanced confidence score calculator
                score, guide, score_label = calculate_confidence_score(grow, brow)
                copied = grow.get('copied_narration')
                if isinstance(copied, dict) and score < 70:
                    # Narration was pasted from an older voucher: its words/UTR are noise,
                    # so rank by amount + date proximity alone.
                    cscore = max(20, 70 - days * 6)
                    if cscore > score:
                        score, score_label = cscore, "Copied Narr · Amt/Date"
                        guide = (f"GL narration looks copied from the {copied.get('original_date')} entry "
                                 f"(UTR {copied.get('utr')} already used) - judge by amount/date/contra account. {days} days apart")
                g_alert, b_alert = grow.get('transfer_alert'), brow.get('transfer_alert')
                if isinstance(g_alert, dict) and isinstance(b_alert, dict) and g_alert.get('id') == b_alert.get('id'):
                    # Never auto-matched by design: the user must confirm the stuck original did not also pay out.
                    score, score_label = max(score, 88), "Re-sent Payment"
                    guide = (f"Re-sent payment to A/c {g_alert.get('benef_acc')} [{g_alert.get('level')}] - "
                             "verify the stuck original did not ALSO go through before linking")

                # Assign visual colors based on score
                if score >= 95:
                    col = "success"  # Green
                elif score >= 80:
                    col = "info"     # Blue (e.g. possible typo)
                elif score >= 50:
                    col = "warning"  # Orange/Yellow
                else:
                    col = "secondary" # Gray
                    
                results.append({
                    'bank_id': brow['bank_id'], 
                    'date': brow['date'],
                    'narration': brow['narration'], 
                    'amount': brow['amount'],
                    'reason': guide, 
                    'color': col, 
                    'confidence_score': score, 
                    'score_label': score_label,
                    'days_apart': days,
                    'similarity': narr_sim
                })
        else:
            results = []
        for u in utrs_of(grow['all_refs']):
            for brow in un_b_by_utr.get(u, []):
                if brow['type_sign'] == grow['type_sign'] or abs(brow['amount'] - grow['amount']) < 0.005:
                    continue
                delta = round(brow['amount'] - grow['amount'], 2)
                results.append({
                    'bank_id': brow['bank_id'], 'date': brow['date'], 'narration': brow['narration'],
                    'amount': brow['amount'], 'color': 'danger', 'confidence_score': 90,
                    'reason': f"Same UTR {u} but amount differs by Rs {delta:+,.2f} (bank {brow['amount']:,.2f} vs GL {grow['amount']:,.2f}) - check GL posting",
                    'score_label': 'UTR · Amt Δ', 'days_apart': abs((brow['dt'] - grow['dt']).days),
                    'similarity': 0, 'amount_delta': delta,
                })
        # Sort suggestions by confidence score (descending), then days_apart (ascending)
        rec['suggestions'] = sorted(results, key=lambda x: (-x['confidence_score'], x['days_apart']))[:5]
        missing_bank.append(rec)

    # Build DQ Stats v2
    match_type_counts = {}
    for m in matches:
        t = m['status']
        match_type_counts[t] = match_type_counts.get(t, 0) + 1

    gl_missing_refs = int((df_gl['ref_id'].isna()).sum())
    bank_missing_refs = int((df_bank['ref_id'].isna()).sum())
    gl_dup_entries = int(df_gl['is_duplicate'].sum()) if 'is_duplicate' in df_gl.columns else 0
    gl_matched_amt = df_gl[df_gl['matched']]['amount'].sum() if not df_gl.empty else 0
    gl_total_amt = df_gl['amount'].sum() if not df_gl.empty else 0
    amt_cov = round((gl_matched_amt / gl_total_amt * 100), 2) if gl_total_amt > 0 else 0
    
    transitive_cms = match_type_counts.get('TIER1.1_TRANSITIVE_CMS', 0)
    enriched_gl_count = int(df_gl.apply(lambda r: len(r.get('all_refs', [])) > len(r.get('original_refs', [])), axis=1).sum()) if not df_gl.empty else 0

    output = {
        'summary': {'matched': len(matches), 'bank_unmatched': len(un_b), 'gl_unmatched': len(un_g),
                    'active_tags': active_tag_list(df_bank, df_gl)},
        'dq_stats': {
            'report_version': '2.1',
            'statement_integrity': check_statement_integrity(data_root),
            'transfer_alerts': mark_settled_alerts(collect_transfer_alerts(df_bank, df_gl, data_root), df_bank, df_gl),
            'outward_status': _sidecar(data_root).get('outward_status'),
            'gl': {
                'total_entries': len(df_gl),
                'matched_entries': int(df_gl['matched'].sum()),
                'unmatched_entries': len(un_g),
                'coverage_pct': round(df_gl['matched'].mean() * 100, 2) if not df_gl.empty else 0,
                'missing_references': gl_missing_refs,
                'duplicate_entries': gl_dup_entries,
                'amount_coverage_pct': amt_cov
            },
            'bank': {
                'total_entries': len(df_bank),
                'matched_entries': int(df_bank['matched'].sum()),
                'unmatched_entries': len(un_b),
                'coverage_pct': round(df_bank['matched'].mean() * 100, 2) if not df_bank.empty else 0,
                'missing_references': bank_missing_refs
            },
            'engine_performance': {
                'transitive_cms_matches': transitive_cms,
                'enriched_gl_entries': enriched_gl_count,
                'match_types': match_type_counts
            }
        },
        'matches': matches,
        'missing_gl': sanitize_dataframe(un_b.drop(columns=['dt'])).to_dict('records'),
        'missing_bank': sanitize_dataframe(pd.DataFrame(missing_bank)).to_dict('records') if missing_bank else []
    }
    storage.save_text(paths['OUTPUT_JSON'], safe_json_dumps(output))
    report(100, "Done."); return output


def active_tag_list(df_bank: pd.DataFrame, df_gl: pd.DataFrame) -> List[str]:
    """Individual channel tags present in the data (not 'A + B' combinations),
    in classifier order so the filter menu reads UPI, IMPS, RTGS, NEFT ..."""
    present = set()
    for df in (df_bank, df_gl):
        if not df.empty and 'txn_type' in df.columns:
            for t in df['txn_type'].dropna().unique():
                present.update(split_tags(t))
    order = [t for t, _ in TXN_TAG_RULES]
    return sorted(present, key=lambda t: order.index(t) if t in order else len(order))

def quick_save(df_bank: Optional[pd.DataFrame] = None, df_gl: Optional[pd.DataFrame] = None, data_root: str = DEFAULT_DATA_ROOT, global_upi_registry: Optional[Set[str]] = None) -> Dict:
    paths = get_paths(data_root)
    if not os.path.exists(paths['OUTPUT_JSON']): return reconcile(df_bank, df_gl, data_root, global_upi_registry=global_upi_registry)
    try:
        # storage.load_json reads UTF-8 explicitly; the old bare open() used the
        # Windows locale codec (cp1252) and would mangle any non-ASCII narration.
        data = storage.load_json(paths['OUTPUT_JSON'])
        if not isinstance(data, dict):
            raise ValueError("data.json is not a reconciliation object")
        if df_bank is None or df_gl is None:
            loaded_bank, loaded_gl = load_all(data_root)
            df_bank = loaded_bank if df_bank is None else df_bank
            df_gl = loaded_gl if df_gl is None else df_gl

        df_bank, df_gl = df_bank.copy(), df_gl.copy()
        df_bank['matched'], df_gl['matched'] = False, False
        if 'duplicate_index' not in df_bank.columns:
            df_bank = compute_duplicates(df_bank, global_upi_registry)
        if 'duplicate_index' not in df_gl.columns:
            df_gl = compute_duplicates(df_gl, global_upi_registry)
        
        # Load Manual Matches first to get their IDs
        manual = []
        manual_b_ids = set()
        manual_g_ids = set()
        if os.path.exists(paths['MANUAL_LINKS_PATH']):
            manual = storage.load_json(paths['MANUAL_LINKS_PATH'], []) or []
            for link in manual:
                if not isinstance(link, dict): continue
                b_ids = link.get('bank_ids', []) or []
                # Expand base bank_ids to include their sequenced versions
                expanded_b_ids = []
                for bid in b_ids:
                    if bid:
                        expanded_b_ids.append(bid)
                        if '_' not in bid:
                            base_rows = df_bank[df_bank['bank_id'] == bid]
                            if not base_rows.empty:
                                base_amt = base_rows.iloc[0]['amount']
                                matches_seq = df_bank[(df_bank['bank_id'].str.startswith(bid + '_')) & (df_bank['amount'] == base_amt)]['bank_id'].tolist()
                                expanded_b_ids.extend(matches_seq)
                manual_b_ids.update(expanded_b_ids)
                manual_g_ids.update(link.get('gl_ids', []) or [])

        cur_matches = []
        
        # Bulk collect auto-match bank and GL IDs to be matched
        auto_b_ids_all = []
        auto_g_ids_all = []
        for m in data.get('matches', []):
            if not isinstance(m, dict): continue
            if m.get('status') == 'MANUAL_MATCH': continue
            
            b_ids = [b['bank_id'] for b in (m.get('bks', []) or []) if isinstance(b, dict) and 'bank_id' in b]
            g_ids = [g['gl_id'] for g in (m.get('gls', []) or []) if isinstance(g, dict) and 'gl_id' in g]
            
            # If any ID in this auto-match is now in a manual match, discard the auto-match
            if any(bid in manual_b_ids for bid in b_ids) or any(gid in manual_g_ids for gid in g_ids):
                continue
            
            cur_matches.append(m)
            auto_b_ids_all.extend(b_ids)
            auto_g_ids_all.extend(g_ids)

        matched_bank_ids_set = set(auto_b_ids_all)
        matched_gl_ids_set = set(auto_g_ids_all)

        # Fast dictionary records lookup
        bank_cols = [c for c in df_bank.columns if c != 'dt']
        gl_cols = [c for c in df_gl.columns if c != 'dt']
        
        bank_dict = {row['bank_id']: row for row in df_bank[bank_cols].to_dict('records') if row.get('bank_id')}
        gl_dict = {row['gl_id']: row for row in df_gl[gl_cols].to_dict('records') if row.get('gl_id')}

        # Now apply manual matches using O(1) key lookups
        for link in manual:
            if not isinstance(link, dict): continue
            b_ids = link.get('bank_ids', []) or []
            g_ids = link.get('gl_ids', []) or []
            
            # Expand base bank_ids to include their sequenced versions
            expanded_b_ids = []
            for bid in b_ids:
                if bid:
                    expanded_b_ids.append(bid)
                    if '_' not in bid:
                        base_amt = bank_dict.get(bid, {}).get('amount')
                        if base_amt is not None:
                            matches_seq = [k for k in bank_dict.keys() if k.startswith(bid + '_') and bank_dict[k].get('amount') == base_amt]
                            expanded_b_ids.extend(matches_seq)
            b_ids = list(set(expanded_b_ids))
            
            b_rows = [bank_dict[bid] for bid in b_ids if bid in bank_dict and bid not in matched_bank_ids_set]
            g_rows = [gl_dict[gid] for gid in g_ids if gid in gl_dict and gid not in matched_gl_ids_set]
            
            if b_rows or g_rows:
                for r in b_rows: matched_bank_ids_set.add(r['bank_id'])
                for r in g_rows: matched_gl_ids_set.add(r['gl_id'])
                
                # Append manual match record (targeted Nan-cleaning on manual matches)
                cur_matches.append({
                    'status': 'MANUAL_MATCH', 
                    'bks': clean_record(b_rows), 
                    'gls': clean_record(g_rows)
                })
        
        # Bulk mark final matched state in dataframes (exactly one scan)
        df_bank['matched'] = df_bank['bank_id'].isin(matched_bank_ids_set)
        df_gl['matched'] = df_gl['gl_id'].isin(matched_gl_ids_set)

        # Structure computed above; only the matched-dependent flag needs a refresh
        df_bank = refresh_duplicate_matched(df_bank)
        df_gl = refresh_duplicate_matched(df_gl)
        gl_cols = [c for c in df_gl.columns if c != 'dt']
        
        un_b, un_g = df_bank[~df_bank['matched']], df_gl[~df_gl['matched']]
        
        # Use suggestions from the original data if available, filtering out matched bank entries
        sug_lookup = {str(item['gl_id']): item.get('suggestions', []) or [] for item in data.get('missing_bank', []) if isinstance(item, dict) and item.get('gl_id')}
        
        # Fast records conversion avoiding iterrows()
        un_g_records = un_g[gl_cols].to_dict('records')
        missing_bank = []
        for rec in un_g_records:
            sugs = sug_lookup.get(str(rec['gl_id']), []) or []
            rec['suggestions'] = [s for s in sugs if isinstance(s, dict) and s.get('bank_id') not in matched_bank_ids_set]
            
            if rec.get('is_duplicate_matched'):
                rec['duplicate_info'] = f"Duplicate {rec['duplicate_index']}: Ref {rec['duplicate_ref']} (PARTNER LINKED)"
            elif rec.get('duplicate_index') is not None:
                rec['duplicate_info'] = f"Duplicate {rec['duplicate_index']}: Ref {rec['duplicate_ref']}"
            
            missing_bank.append(rec)
            
        data.update({
            'summary': {
                'matched': len(cur_matches), 
                'bank_unmatched': len(un_b), 
                'gl_unmatched': len(un_g),
                'active_tags': data.get('summary', {}).get('active_tags', [])
            }, 
            'matches': cur_matches, 
            # Targeted Nan-cleaning on unmatched lists only
            'missing_gl': sanitize_dataframe(un_b[bank_cols]).to_dict('records'), 
            'missing_bank': sanitize_dataframe(pd.DataFrame(missing_bank)).to_dict('records') if missing_bank else []
        })
        
        if 'dq_stats' in data:
            if not isinstance(data['dq_stats'], dict): data['dq_stats'] = {}
            if 'gl' not in data['dq_stats'] or not isinstance(data['dq_stats']['gl'], dict): data['dq_stats']['gl'] = {}
            if 'bank' not in data['dq_stats'] or not isinstance(data['dq_stats']['bank'], dict): data['dq_stats']['bank'] = {}
            data['dq_stats']['gl'].update({'matched_entries': int(df_gl['matched'].sum()), 'unmatched_entries': len(un_g)})
            data['dq_stats']['bank'].update({'matched_entries': int(df_bank['matched'].sum()), 'unmatched_entries': len(un_b)})
            # A manual link can settle (or un-settle) a re-send group.
            data['dq_stats']['transfer_alerts'] = mark_settled_alerts(collect_transfer_alerts(df_bank, df_gl, data_root), df_bank, df_gl)
            if _sidecar(data_root).get('outward_status') is not None:
                data['dq_stats']['outward_status'] = _sidecar(data_root)['outward_status']

        # Atomic swap; skips recursive clean_record on massive dict
        storage.save_text(paths['OUTPUT_JSON'], safe_json_dumps(data))
        return data
    except Exception as e:
        logger.error(f"Quick Save failed: {e}", exc_info=True)
        return reconcile(df_bank, df_gl, data_root, global_upi_registry=global_upi_registry)



if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--data_root', default=DEFAULT_DATA_ROOT)
    args = p.parse_args()
    res = reconcile(data_root=args.data_root)
    print(f"Match: {res['summary']['matched']}, Bank: {res['summary']['bank_unmatched']}, GL: {res['summary']['gl_unmatched']}")
