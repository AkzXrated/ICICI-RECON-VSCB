from flask import Flask, render_template, jsonify, request, send_from_directory, session, Response
import gzip
import json
import logging
import os
import re
import secrets
import reconciler
import storage
from datetime import datetime
from io import BytesIO
import time
import pandas as pd

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _get_secret_key():
    """Prefer env var; otherwise persist a generated key so sessions survive restarts."""
    key = os.environ.get('ICICI_GL_SECRET_KEY')
    if key:
        return key
    key_file = os.path.join(BASE_DIR, '.secret_key')
    try:
        if os.path.exists(key_file):
            with open(key_file, 'r', encoding='utf-8') as f:
                key = f.read().strip()
            if key:
                return key
        key = secrets.token_hex(32)
        with open(key_file, 'w', encoding='utf-8') as f:
            f.write(key)
        return key
    except OSError as e:
        logger.warning("Could not persist secret key (%s); using per-run key.", e)
        return secrets.token_hex(32)

app = Flask(__name__)
app.secret_key = _get_secret_key()
# Use NaN-safe JSON encoder for ALL Flask responses
app.json.sort_keys = False
app.json_encoder = reconciler.SafeJSONEncoder  # Flask < 2.3 compat

# For Flask 2.3+ override the json provider
import math
from flask.json.provider import DefaultJSONProvider
class SafeJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        return super().default(obj)
    def dumps(self, obj, **kwargs):
        # Recursively clean NaN before serialization
        obj = reconciler.clean_record(obj)
        return super().dumps(obj, **kwargs)

app.json_provider_class = SafeJSONProvider
app.json = SafeJSONProvider(app)

@app.after_request
def compress_response(response):
    """Gzip JSON responses (the /api/data payload can run into the
    multi-MB range at 90k+ rows). Skips SSE/streaming and already-encoded
    responses; falls back silently if compression wouldn't help."""
    if (response.direct_passthrough or response.is_streamed or
            not (200 <= response.status_code < 300) or
            'Content-Encoding' in response.headers):
        return response
    if 'gzip' not in request.headers.get('Accept-Encoding', '').lower():
        return response
    data = response.get_data()
    if len(data) < 1024:
        return response
    buf = BytesIO()
    with gzip.GzipFile(mode='wb', fileobj=buf, compresslevel=6) as gz:
        gz.write(data)
    compressed = buf.getvalue()
    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = str(len(compressed))
    vary = response.headers.get('Vary', '')
    if 'Accept-Encoding' not in vary:
        response.headers['Vary'] = f"{vary}, Accept-Encoding".lstrip(', ')
    return response

DATA_ROOT_DIR = os.path.join(BASE_DIR, 'Data')

def get_regular_upis():
    registry_file = os.path.join(DATA_ROOT_DIR, 'upi_knowledge_base.json')
    try:
        registry = storage.load_json(registry_file, {}) or {}
        return {k for k, v in registry.items() if v.get('count', 0) > 1 or len(v.get('fys', [])) > 1}
    except storage.CorruptJSONError as e:
        logger.error("UPI registry unreadable: %s", e)
    return set()

def update_upi_registry(fy, df_bank, df_gl):
    registry_file = os.path.join(DATA_ROOT_DIR, 'upi_knowledge_base.json')
    with storage.path_lock(registry_file):
        try:
            registry = storage.load_json(registry_file, {}) or {}
        except storage.CorruptJSONError as e:
            logger.error("UPI registry unreadable, starting fresh: %s", e)
            registry = {}

        current_counts = {}
        party_names = {}
        for df in [df_bank, df_gl]:
            if df is not None and not df.empty and 'upi_id' in df.columns:
                has_party = 'party_name' in df.columns
                for _, row in df.iterrows():
                    upi = row.get('upi_id')
                    if pd.notna(upi) and str(upi).strip() != '':
                        u_str = str(upi).strip().upper()
                        current_counts[u_str] = current_counts.get(u_str, 0) + 1
                        if has_party:
                            p = row.get('party_name')
                            if pd.notna(p):
                                p_str = str(p).strip().upper()
                                if p_str and p_str not in ('NONE', 'NAN') and '@' not in p_str:
                                    party_names[u_str] = p_str

        changed = False
        for upi, count in current_counts.items():
            p_name = party_names.get(upi)
            if upi not in registry:
                registry[upi] = {
                    "count": count,
                    "first_seen": fy,
                    "last_seen": fy,
                    "fys": [fy],
                    "party_name": p_name
                }
                changed = True
            else:
                entry = registry[upi]
                if fy not in entry.get("fys", []):
                    entry.setdefault("fys", []).append(fy)
                    entry["count"] = entry.get("count", 0) + count
                    entry["last_seen"] = fy
                    changed = True
                if p_name and not entry.get("party_name"):
                    entry["party_name"] = p_name
                    changed = True
        if changed:
            try:
                storage.save_json(registry_file, registry)
            except OSError as e:
                logger.error("Could not save UPI registry: %s", e)

        return set(registry.keys())

# Cache for dataframes to speed up reconciliation, keyed by FY
CACHE = {}
# Global Progress Tracker
PROGRESS = {
    "percent": 0,
    "message": "Ready",
    "start_time": None,
    "active": False
}

def get_current_fy():
    return session.get('fy')

# Tallied (fully reconciled, rarely-touched) FYs are archived one level
# deeper under Data/Tallied/<fy> to keep the landing page focused on active
# years. Their identifier (session value, __ALL__ prefix key, cache key)
# stays the bare directory name, unchanged from before the move.
TALLIED_DIR_NAME = 'Tallied'

def list_fy_dirs():
    """Return {fy_name: (full_path, is_tallied)} for every FY directory,
    scanning both Data/ directly and the archived Data/Tallied/ subfolder."""
    result = {}
    if not os.path.isdir(DATA_ROOT_DIR):
        return result
    for d in os.listdir(DATA_ROOT_DIR):
        if d == TALLIED_DIR_NAME:
            continue
        full = os.path.join(DATA_ROOT_DIR, d)
        if os.path.isdir(full):
            result[d] = (full, False)
    tallied_root = os.path.join(DATA_ROOT_DIR, TALLIED_DIR_NAME)
    if os.path.isdir(tallied_root):
        for d in os.listdir(tallied_root):
            full = os.path.join(tallied_root, d)
            if os.path.isdir(full):
                result[d] = (full, True)
    return result

def is_safe_fy(fy):
    """A valid FY is exactly the name of an existing directory under Data/
    or Data/Tallied/ — no separators, no '..', no drive prefixes (blocks
    path traversal)."""
    if not fy or not isinstance(fy, str):
        return False
    if fy in ('.', '..') or fy != os.path.basename(fy):
        return False
    return fy in list_fy_dirs()

def get_fy_path(fy):
    dirs = list_fy_dirs()
    if not is_safe_fy(fy) or fy not in dirs:
        raise ValueError(f"Invalid financial year: {fy!r}")
    return dirs[fy][0]

def file_etag(path):
    """A cheap ETag from mtime+size — lets a repeat /api/data request for an
    unchanged cache short-circuit to a 304 before any JSON parse/serialize."""
    try:
        stat = os.stat(path)
        return f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    except OSError:
        return None

def get_cached_data(fy):
    if fy not in CACHE:
        CACHE[fy] = {'df_bank': None, 'df_gl': None}
    
    if CACHE[fy]['df_bank'] is None or CACHE[fy]['df_gl'] is None:
        fy_path = get_fy_path(fy)
        CACHE[fy]['df_bank'] = reconciler.load_bank_psv(fy_path)
        rtgs = reconciler.load_rtgs_reports(fy_path)
        cms = reconciler.load_cms_reports(fy_path)
        upi_reports = reconciler.load_upi_collection_reports(fy_path)
        CACHE[fy]['df_bank'] = reconciler.enrich_bank_with_upi_reports(CACHE[fy]['df_bank'], upi_reports)
        CACHE[fy]['df_gl'] = reconciler.load_gl_xml(fy_path, rtgs, cms)
        CACHE[fy]['df_gl'] = reconciler.enrich_gl_with_upi_reports(CACHE[fy]['df_gl'], upi_reports)
    return CACHE[fy]['df_bank'].copy(), CACHE[fy]['df_gl'].copy()

def update_progress(pct, msg):
    PROGRESS["percent"] = pct
    PROGRESS["message"] = msg
    if pct == 0:
        PROGRESS["start_time"] = time.time()
        PROGRESS["active"] = True
    elif pct >= 100:
        PROGRESS["active"] = False

@app.route('/')
def index():
    # Reset progress state to avoid stale SSE data.
    # (Session FY is kept so a page refresh restores the working view;
    # the in-memory frame cache is also kept — clearing it here forced
    # full disk re-loads on every stray refresh.)
    PROGRESS["percent"] = 0
    PROGRESS["message"] = "Ready"
    PROGRESS["active"] = False
    return render_template('index.html')

@app.route('/api/current-fy')
def current_fy():
    fy = get_current_fy()
    if fy and fy != '__ALL__' and not is_safe_fy(fy):
        # Stale session pointing at a renamed/removed FY directory
        session.pop('fy', None)
        fy = None
    return jsonify({"fy": fy})

@app.route('/api/progress-stream')
def progress_stream():
    def generate():
        while True:
            yield f"data: {json.dumps(PROGRESS)}\n\n"
            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/fys')
def list_fys():
    dirs = list_fy_dirs()
    fys_sorted = sorted(dirs.keys(), reverse=True)
    res = []
    for fy in fys_sorted:
        tag = ""
        if " - OLD" in fy:
            tag = "OLD"
        elif " - PRESENT" in fy:
            tag = "PRESENT"

        # Archived under Data/Tallied/ is the source of truth for "tallied"
        tallied = dirs[fy][1]

        res.append({
            "value": fy,
            "label": fy.split(" - ")[0] if " - " in fy else fy,
            "tag": tag,
            "tallied": tallied
        })
    return jsonify(res)

@app.route('/api/set-fy', methods=['POST'])
def set_fy():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid or missing JSON body"}), 400
    fy = data.get('fy')
    if not fy:
        return jsonify({"error": "No FY provided"}), 400
    if fy != '__ALL__' and not is_safe_fy(fy):
        return jsonify({"error": "Unknown financial year"}), 400
    session['fy'] = fy
    return jsonify({"status": "success", "fy": fy})

@app.route('/api/data')
def get_data():
    fy = get_current_fy()
    if not fy:
        return jsonify({"error": "No financial year selected"}), 400
    
    if fy == '__ALL__':
        fys_sorted = sorted(list_fy_dirs().keys(), reverse=True)

        aggregated = {
            "missing_bank": [],
            "missing_gl": [],
            "matches": [],
            "summary": {
                "matched": 0,
                "bank_unmatched": 0,
                "gl_unmatched": 0,
                "bank_total": 0,
                "gl_total": 0,
                "matched_bank_amount": 0.0,
                "matched_gl_amount": 0.0,
                "unmatched_bank_amount": 0.0,
                "unmatched_gl_amount": 0.0
            },
            "multi_fy": True
        }
        
        all_active_tags = set()
        for f in fys_sorted:
            fy_path = get_fy_path(f)
            output_json = os.path.join(fy_path, 'data.json')
            
            result = None
            try:
                result = storage.load_json(output_json)
            except storage.CorruptJSONError as e:
                logger.error("Cache for %s unreadable, recomputing: %s", f, e)

            if result is None:
                df_b, df_g = get_cached_data(f)
                result = reconciler.reconcile(df_bank=df_b, df_gl=df_g, data_root=fy_path)
            
            prefix = f"{f}:"
            fy_label = f.split(" - ")[0] if " - " in f else f
            
            def prefix_item(item):
                if not item: return item
                new_item = item.copy()
                if 'bank_id' in new_item and new_item['bank_id']:
                    new_item['bank_id'] = prefix + str(new_item['bank_id'])
                if 'gl_id' in new_item and new_item['gl_id']:
                    new_item['gl_id'] = prefix + str(new_item['gl_id'])
                new_item['id'] = prefix + str(item.get('id', item.get('bank_id') or item.get('gl_id') or ''))
                new_item['fy'] = fy_label
                if 'matched_with' in new_item and isinstance(new_item['matched_with'], list):
                    new_item['matched_with'] = [prefix + str(x) for x in new_item['matched_with']]
                return new_item
                
            for b in result.get('missing_bank', []):
                aggregated['missing_bank'].append(prefix_item(b))
                
            for g in result.get('missing_gl', []):
                aggregated['missing_gl'].append(prefix_item(g))
                
            for m in result.get('matches', []):
                match_status = m.get('status') or m.get('type', 'GROUP_MATCH')
                new_m = {
                    'bks': [prefix_item(b) for b in m.get('bks', [])],
                    'gls': [prefix_item(g) for g in m.get('gls', [])],
                    'type': m.get('type') or match_status,
                    'status': match_status,
                    'timestamp': m.get('timestamp'),
                    'audit': m.get('audit')
                }
                aggregated['matches'].append(new_m)
                
            s = result.get('summary', {})
            for key in aggregated['summary']:
                if key in s:
                    aggregated['summary'][key] += s[key]
            for tag in s.get('active_tags', []):
                all_active_tags.add(tag)

        aggregated['summary']['active_tags'] = sorted(list(all_active_tags))
                    
        return jsonify(aggregated)

    fy_path = get_fy_path(fy)
    output_json = os.path.join(fy_path, 'data.json')

    if os.path.exists(output_json):
        etag = file_etag(output_json)
        if etag and request.headers.get('If-None-Match') == etag:
            # Unchanged since last load — skip the JSON parse/serialize entirely.
            return Response(status=304)
        update_progress(0, "Loading saved reconciliation...")
        try:
            result = storage.load_json(output_json)
            if result is not None:
                update_progress(100, "Complete")
                resp = jsonify(result)
                if etag:
                    resp.headers['ETag'] = etag
                    resp.headers['Cache-Control'] = 'no-cache'
                return resp
        except storage.CorruptJSONError as e:
            logger.error("Cache for %s unreadable, running full reconcile: %s", fy, e)


    update_progress(0, "Starting Reconciliation...")
    df_b, df_g = get_cached_data(fy)
    reg_upis = get_regular_upis()
    result = reconciler.reconcile(df_bank=df_b, df_gl=df_g, data_root=fy_path, progress_cb=update_progress, global_upi_registry=reg_upis)
    update_upi_registry(fy, df_b, df_g)
    update_progress(100, "Complete")
    resp = jsonify(result)
    etag = file_etag(output_json)
    if etag:
        resp.headers['ETag'] = etag
        resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/api/refresh', methods=['POST'])
def refresh():
    fy = get_current_fy()
    if not fy:
        return jsonify({"error": "No financial year selected"}), 400
    
    update_progress(0, "Reloading Data...")
    fy_path = get_fy_path(fy)
    if fy not in CACHE:
        CACHE[fy] = {}
    CACHE[fy]['df_bank'] = reconciler.load_bank_psv(fy_path)
    rtgs = reconciler.load_rtgs_reports(fy_path)
    cms = reconciler.load_cms_reports(fy_path)
    upi_reports = reconciler.load_upi_collection_reports(fy_path)
    CACHE[fy]['df_bank'] = reconciler.enrich_bank_with_upi_reports(CACHE[fy]['df_bank'], upi_reports)
    CACHE[fy]['df_gl'] = reconciler.load_gl_xml(fy_path, rtgs, cms)
    CACHE[fy]['df_gl'] = reconciler.enrich_gl_with_upi_reports(CACHE[fy]['df_gl'], upi_reports)
    
    reg_upis = get_regular_upis()
    result = reconciler.reconcile(df_bank=CACHE[fy]['df_bank'].copy(), df_gl=CACHE[fy]['df_gl'].copy(), data_root=fy_path, progress_cb=update_progress, global_upi_registry=reg_upis)
    update_upi_registry(fy, CACHE[fy]['df_bank'], CACHE[fy]['df_gl'])
    update_progress(100, "Complete")
    return jsonify(result)

@app.route('/api/manual-match', methods=['POST'])
def manual_match():
    fy = get_current_fy()
    if not fy:
        return jsonify({"error": "No financial year selected"}), 400
    if fy == '__ALL__':
        return jsonify({"error": "Read-only mode. Cannot match transactions in All Financial Years view."}), 400
    if not is_safe_fy(fy):
        return jsonify({"error": "Invalid financial year"}), 400
    fy_path = get_fy_path(fy)
    matches_file = os.path.join(fy_path, 'manual_links.json')
    
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid or missing JSON body"}), 400
        
        b_in = data.get('bankIds', [])
        g_in = data.get('glIds', [])
        if not isinstance(b_in, (list, tuple, set)):
            b_in = []
        if not isinstance(g_in, (list, tuple, set)):
            g_in = []

        bank_ids = [str(i).strip() for i in b_in if i is not None and str(i).strip()]
        gl_ids = [str(i).strip() for i in g_in if i is not None and str(i).strip()]
        match_type = data.get('type', 'GROUP_MATCH')

        if not bank_ids and not gl_ids:
            return jsonify({"error": "No IDs provided"}), 400

        with storage.path_lock(matches_file):
            try:
                matches = storage.load_json(matches_file, []) or []
            except storage.CorruptJSONError:
                # Never overwrite an unreadable links file — that would wipe all manual history.
                return jsonify({"error": "manual_links.json is unreadable; match NOT saved. "
                                         "Restore it from manual_links.json.bak and retry."}), 500

            for m in matches:
                if set(m.get('bank_ids', [])) == set(bank_ids) and set(m.get('gl_ids', [])) == set(gl_ids):
                    return jsonify({"status": "already_exists", "message": "This match already exists"}), 200

            new_match = {
                'type': match_type,
                'bank_ids': bank_ids,
                'gl_ids': gl_ids,
                'timestamp': datetime.now().isoformat()
            }
            matches.append(new_match)
            storage.save_json(matches_file, matches)

        # Narrative Learning (Alias Engine) - Enhanced to prevent poisoning
        if len(bank_ids) == 1 and len(gl_ids) == 1:
            df_b, df_g = get_cached_data(fy)
            b_row = df_b[df_b['bank_id'] == bank_ids[0]]
            g_row = df_g[df_g['gl_id'] == gl_ids[0]]
            
            if not b_row.empty and not g_row.empty:
                b_narr_orig = b_row.iloc[0]['narration']
                g_narr_orig = g_row.iloc[0]['narration']
                b_narr_clean = reconciler.clean_narration(b_narr_orig)
                g_narr_clean = reconciler.clean_narration(g_narr_orig)
                
                # Check for sticky note wrong narration feedback
                notes_file = os.path.join(fy_path, 'sticky_notes.json')
                has_wrong_narration_note = False
                try:
                    notes = storage.load_json(notes_file, {}) or {}
                    for tx_id in [bank_ids[0], gl_ids[0]]:
                        if tx_id in notes:
                            note = notes[tx_id]
                            if isinstance(note, dict) and (note.get('category') == 'Wrong Narration' or str(note.get('type', '')).upper() == 'WRONG_NARRATION'):
                                has_wrong_narration_note = True
                            elif isinstance(note, str) and 'wrong narration' in note.lower():
                                has_wrong_narration_note = True
                except storage.CorruptJSONError as e:
                    logger.error("Sticky notes unreadable during alias learning: %s", e)
                
                # Validation: Only learn if they share a significant block, have baseline similarity, or have wrong narration note
                shared_blocks = set(re.findall(r'[A-Z0-9]{5,}', b_narr_clean)) & set(re.findall(r'[A-Z0-9]{5,}', g_narr_clean))
                
                import difflib
                similarity = difflib.SequenceMatcher(None, b_narr_clean, g_narr_clean).ratio()
                
                if (shared_blocks or similarity > 0.4 or has_wrong_narration_note) and b_narr_clean != g_narr_clean:
                    alias_file = os.path.join(DATA_ROOT_DIR, 'narrative_aliases.json')
                    with storage.path_lock(alias_file):
                        try:
                            aliases = storage.load_json(alias_file, {}) or {}
                        except storage.CorruptJSONError as e:
                            logger.error("Alias file unreadable; skipping alias learning: %s", e)
                            aliases = None
                        if aliases is not None:
                            # Store clean versions as keys/values for the matching engine
                            if g_narr_clean not in aliases: aliases[g_narr_clean] = []
                            if b_narr_clean not in aliases[g_narr_clean]:
                                aliases[g_narr_clean].append(b_narr_clean)
                                storage.save_json(alias_file, aliases)

        update_progress(0, "Re-calculating Manual Match impact...")
        df_b, df_g = get_cached_data(fy)
        reg_upis = get_regular_upis()
        result = reconciler.quick_save(df_bank=df_b, df_gl=df_g, data_root=fy_path, global_upi_registry=reg_upis)
        update_progress(100, "Complete")
        return jsonify(result)
    except Exception as e:
        logger.exception("manual_match failed")
        return jsonify({"error": str(e)}), 500

@app.route('/api/unlink', methods=['POST'])
def unlink_match():
    fy = get_current_fy()
    if not fy:
        return jsonify({"error": "No financial year selected"}), 400
    if fy == '__ALL__':
        return jsonify({"error": "Read-only mode. Cannot unlink transactions in All Financial Years view."}), 400
    if not is_safe_fy(fy):
        return jsonify({"error": "Invalid financial year"}), 400
    fy_path = get_fy_path(fy)
    matches_file = os.path.join(fy_path, 'manual_links.json')

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid or missing JSON body"}), 400
    all_manual = data.get('allManual', False)

    df_b, df_g = get_cached_data(fy)

    if all_manual:
        try:
            # backup=True keeps the wiped set recoverable in manual_links.json.bak
            storage.save_json(matches_file, [])
            return jsonify(reconciler.quick_save(df_bank=df_b, df_gl=df_g, data_root=fy_path))
        except Exception as e:
            logger.exception("unlink all failed")
            return jsonify({"error": str(e)}), 500

    b_in = data.get('bankIds', [])
    g_in = data.get('glIds', [])
    if not isinstance(b_in, (list, tuple, set)):
        b_in = []
    if not isinstance(g_in, (list, tuple, set)):
        g_in = []

    bank_ids = set(str(i).strip() for i in b_in if i is not None and str(i).strip())
    gl_ids = set(str(i).strip() for i in g_in if i is not None and str(i).strip())

    if not bank_ids and not gl_ids:
        return jsonify({"error": "No IDs provided to unlink"}), 400

    if not os.path.exists(matches_file):
        return jsonify({"error": "No manual matches found"}), 404

    try:
        with storage.path_lock(matches_file):
            try:
                matches = storage.load_json(matches_file, []) or []
            except storage.CorruptJSONError:
                return jsonify({"error": "manual_links.json is unreadable; unlink NOT applied. "
                                         "Restore it from manual_links.json.bak and retry."}), 500

            new_matches = []
            removed = False
            for m in matches:
                m_bks = set(m.get('bank_ids', []))
                m_gls = set(m.get('gl_ids', []))

                if bank_ids == m_bks and gl_ids == m_gls:
                    removed = True
                    continue

                if (bank_ids & m_bks) or (gl_ids & m_gls):
                    if bank_ids & m_bks:
                        m['bank_ids'] = list(m_bks - bank_ids)
                        removed = True
                    if gl_ids & m_gls:
                        m['gl_ids'] = list(m_gls - gl_ids)
                        removed = True

                    # Validate that the match remains structurally sound (double-entry or valid internal offset)
                    is_valid = (len(m['bank_ids']) > 0 and len(m['gl_ids']) > 0) or \
                               (len(m['bank_ids']) >= 2 and len(m['gl_ids']) == 0) or \
                               (len(m['gl_ids']) >= 2 and len(m['bank_ids']) == 0)
                    if is_valid:
                        new_matches.append(m)
                else:
                    new_matches.append(m)

            if not removed:
                return jsonify({"error": "Match not found"}), 404

            storage.save_json(matches_file, new_matches)

        reg_upis = get_regular_upis()
        return jsonify(reconciler.quick_save(df_bank=df_b, df_gl=df_g, data_root=fy_path, global_upi_registry=reg_upis))
    except Exception as e:
        logger.exception("unlink failed")
        return jsonify({"error": str(e)}), 500

VALID_NOTE_TYPES = {'NOTE', 'WARNING', 'ACTION', 'AUDIT', 'TAX', 'QUERY', 'WRONG_NARRATION'}
VALID_NOTE_SEVERITIES = {'info', 'low', 'medium', 'high', 'critical'}
VALID_NOTE_STATUSES = {'open', 'resolved'}

def normalize_note_dict(v):
    if isinstance(v, str):
        v = {"text": v, "category": "Remark"}
    elif not isinstance(v, dict):
        v = {"text": str(v), "category": "Remark"}

    text = str(v.get('text', '') or '')
    category = str(v.get('category', 'Remark') or 'Remark')

    note_type = str(v.get('type', '') or '').upper()
    if not note_type:
        cat_lower = category.lower()
        if 'wrong narration' in cat_lower or 'narration' in cat_lower:
            note_type = 'WRONG_NARRATION'
        elif any(w in cat_lower for w in ('warning', 'important', 'risk', 'discrepancy')):
            note_type = 'WARNING'
        elif any(w in cat_lower for w in ('action', 'fix', 'entry', 'adjustment')):
            note_type = 'ACTION'
        elif any(w in cat_lower for w in ('audit', 'verified', 'signoff')):
            note_type = 'AUDIT'
        elif any(w in cat_lower for w in ('tax', 'tds', 'gst')):
            note_type = 'TAX'
        elif any(w in cat_lower for w in ('query', 'pending', 'clarification')):
            note_type = 'QUERY'
        else:
            note_type = 'NOTE'

    if note_type not in VALID_NOTE_TYPES:
        note_type = 'NOTE'

    severity = str(v.get('severity', 'info') or 'info').lower()
    if severity not in VALID_NOTE_SEVERITIES:
        severity = 'info'

    status = str(v.get('status', 'open') or 'open').lower()
    if status not in VALID_NOTE_STATUSES:
        status = 'open'

    created_at = v.get('created_at') or datetime.now().isoformat()
    updated_at = v.get('updated_at') or created_at
    author = str(v.get('author', 'User') or 'User')
    tags = [str(t).strip() for t in v.get('tags', []) if str(t).strip()] if isinstance(v.get('tags'), list) else []

    return {
        "text": text,
        "type": note_type,
        "category": category,
        "severity": severity,
        "status": status,
        "author": author,
        "created_at": created_at,
        "updated_at": updated_at,
        "tags": tags
    }

@app.route('/api/sticky-notes')
def get_sticky_notes():
    fy = get_current_fy()
    if not fy:
        return jsonify({"error": "No financial year selected"}), 400

    if fy == '__ALL__':
        aggregated_notes = {}
        for f in list_fy_dirs().keys():
            notes_file = os.path.join(get_fy_path(f), 'sticky_notes.json')
            try:
                notes = storage.load_json(notes_file, {}) or {}
            except storage.CorruptJSONError as e:
                logger.error("Skipping unreadable sticky notes for %s: %s", f, e)
                continue
            for k, v in notes.items():
                aggregated_notes[f"{f}:{k}"] = normalize_note_dict(v)
        return jsonify(aggregated_notes)

    if not is_safe_fy(fy):
        return jsonify({"error": "Invalid financial year"}), 400

    fy_path = get_fy_path(fy)
    notes_file = os.path.join(fy_path, 'sticky_notes.json')
    try:
        with storage.path_lock(notes_file):
            notes = storage.load_json(notes_file, {}) or {}
            migrated = False
            normalized_notes = {}
            for k, v in notes.items():
                norm = normalize_note_dict(v)
                normalized_notes[k] = norm
                if norm != v:
                    migrated = True
            if migrated:
                storage.save_json(notes_file, normalized_notes)
        return jsonify(normalized_notes)
    except Exception as e:
        logger.exception("get_sticky_notes failed")
        return jsonify({"error": str(e)}), 500

@app.route('/api/sticky-notes', methods=['POST'])
def save_sticky_note():
    fy = get_current_fy()
    if not fy:
        return jsonify({"error": "No financial year selected"}), 400
    if fy == '__ALL__':
        return jsonify({"error": "Cannot edit notes in read-only All Financial Years view."}), 400
    if not is_safe_fy(fy):
        return jsonify({"error": "Invalid financial year"}), 400
    fy_path = get_fy_path(fy)
    notes_file = os.path.join(fy_path, 'sticky_notes.json')
    
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid or missing JSON body"}), 400
    tx_id = data.get('id')
    if tx_id is not None:
        tx_id = str(tx_id).strip()
    if not tx_id:
        return jsonify({"error": "No transaction ID provided"}), 400

    note_text = str(data.get('note', '') or '').strip()
    category = str(data.get('category', '') or '').strip()
    note_type = str(data.get('type', '') or '').strip().upper()
    severity = str(data.get('severity', '') or '').strip().lower()
    status = str(data.get('status', '') or '').strip().lower()
    author = str(data.get('author', 'User') or 'User').strip()
    tags = data.get('tags', [])
    if not isinstance(tags, list): tags = []

    if len(note_text) > 5000:
        return jsonify({"error": "Note too long (max 5000 characters)"}), 400

    with storage.path_lock(notes_file):
        try:
            notes = storage.load_json(notes_file, {}) or {}
        except storage.CorruptJSONError:
            return jsonify({"error": "sticky_notes.json is unreadable; note NOT saved. "
                                     "Restore it from sticky_notes.json.bak and retry."}), 500

        if note_text:
            existing = notes.get(tx_id)
            existing_dict = normalize_note_dict(existing) if existing else {}
            created_at = existing_dict.get('created_at') or datetime.now().isoformat()
            
            note_obj = {
                "text": note_text,
                "type": note_type if note_type in VALID_NOTE_TYPES else (existing_dict.get('type') or 'NOTE'),
                "category": category if category else (existing_dict.get('category') or "Remark"),
                "severity": severity if severity in VALID_NOTE_SEVERITIES else (existing_dict.get('severity') or "info"),
                "status": status if status in VALID_NOTE_STATUSES else (existing_dict.get('status') or "open"),
                "author": author or existing_dict.get('author') or 'User',
                "created_at": created_at,
                "updated_at": datetime.now().isoformat(),
                "tags": tags or existing_dict.get('tags') or []
            }
            notes[tx_id] = note_obj
        else:
            notes.pop(tx_id, None)

        try:
            storage.save_json(notes_file, notes)
            return jsonify({"status": "success", "notes": notes})
        except Exception as e:
            logger.exception("save_sticky_note failed")
            return jsonify({"error": str(e)}), 500

@app.route('/api/sticky-notes/batch', methods=['POST'])
def batch_sticky_notes():
    fy = get_current_fy()
    if not fy:
        return jsonify({"error": "No financial year selected"}), 400
    if fy == '__ALL__':
        return jsonify({"error": "Cannot batch edit notes in read-only All Financial Years view."}), 400
    if not is_safe_fy(fy):
        return jsonify({"error": "Invalid financial year"}), 400
    fy_path = get_fy_path(fy)
    notes_file = os.path.join(fy_path, 'sticky_notes.json')
    
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid or missing JSON body"}), 400
        
    ids = data.get('ids', [])
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "No transaction IDs provided"}), 400
        
    action = str(data.get('action', 'set') or 'set').lower()
    mode = str(data.get('mode', 'replace') or 'replace').lower()
    note_text = str(data.get('note', '') or '').strip()
    category = str(data.get('category', '') or '').strip()
    note_type = str(data.get('type', '') or '').strip().upper()
    severity = str(data.get('severity', '') or '').strip().lower()
    status = str(data.get('status', '') or '').strip().lower()
    author = str(data.get('author', 'User') or 'User').strip()
    tags = data.get('tags', [])
    if not isinstance(tags, list): tags = []

    if len(note_text) > 5000:
        return jsonify({"error": "Note too long (max 5000 characters)"}), 400

    clean_ids = [str(x).strip() for x in ids if str(x).strip()]
    if not clean_ids:
        return jsonify({"error": "No valid transaction IDs"}), 400

    with storage.path_lock(notes_file):
        try:
            notes = storage.load_json(notes_file, {}) or {}
        except storage.CorruptJSONError:
            return jsonify({"error": "sticky_notes.json is unreadable. Restore from backup and retry."}), 500

        now_iso = datetime.now().isoformat()

        if action == 'delete':
            for tx_id in clean_ids:
                notes.pop(tx_id, None)
        elif action == 'resolve':
            for tx_id in clean_ids:
                if tx_id in notes:
                    obj = normalize_note_dict(notes[tx_id])
                    obj['status'] = 'resolved'
                    obj['updated_at'] = now_iso
                    notes[tx_id] = obj
        else: # 'set'
            for tx_id in clean_ids:
                existing = notes.get(tx_id)
                existing_dict = normalize_note_dict(existing) if existing else {}
                created_at = existing_dict.get('created_at') or now_iso
                
                final_text = note_text
                if mode == 'append' and existing_dict.get('text'):
                    final_text = f"{existing_dict['text']}\n{note_text}".strip() if note_text else existing_dict['text']

                final_category = category or existing_dict.get('category') or 'Remark'
                final_type = note_type if note_type in VALID_NOTE_TYPES else (existing_dict.get('type') or 'NOTE')
                final_severity = severity if severity in VALID_NOTE_SEVERITIES else (existing_dict.get('severity') or 'info')
                final_status = status if status in VALID_NOTE_STATUSES else (existing_dict.get('status') or 'open')

                merged_tags = list(set(existing_dict.get('tags', []) + tags))

                notes[tx_id] = {
                    "text": final_text,
                    "type": final_type,
                    "category": final_category,
                    "severity": final_severity,
                    "status": final_status,
                    "author": author or existing_dict.get('author') or 'User',
                    "created_at": created_at,
                    "updated_at": now_iso,
                    "tags": merged_tags
                }

        try:
            storage.save_json(notes_file, notes)
            return jsonify({"status": "success", "count": len(clean_ids), "notes": notes})
        except Exception as e:
            logger.exception("batch_sticky_notes failed")
            return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    # Cleanup data.json on startup to avoid stale first-load (globally if exists)
    if os.path.exists(os.path.join(BASE_DIR, 'data.json')):
        try: os.remove(os.path.join(BASE_DIR, 'data.json'))
        except OSError as e: logger.warning("Could not remove stale data.json: %s", e)
    # Debug (Werkzeug console = remote code execution) must be opted into explicitly.
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    port = int(os.environ.get('ICICI_GL_PORT', '5000'))
    app.run(debug=debug, port=port)
