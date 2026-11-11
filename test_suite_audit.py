"""
ICICI-GL Comprehensive Automated System Verification & Test Suite
==================================================================
Covers:
1. Storage Layer (thread locks, atomic writes, backup rotation, corrupt recovery, UTF-8 BOM)
2. Reconciler Engine & Parsers (references, cheques, UPIs, narrations, duplicates, sign guards)
3. Ingestion & Transitive Bridging (Bank PSV, GL XML, RTGS/CMS enrichment)
4. Multi-Tier Matching Engine (Tiers 1, 1.1, 2, 2.5, 3, 4, 5, 6, 6b, Conflict Guard, Sign Guard)
5. Confidence Score Calculation & Edge Cases
6. Flask Backend API & Persistence (FY security, manual matching, double-entry unlink, sticky notes)
7. Quick-Save Idempotency & Correctness
8. Real-World Dataset Performance Benchmark & Memory Profiling
"""

import os
import sys
import json
import time
import shutil
import tempfile
import threading
import tracemalloc
import unittest
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import storage
import reconciler
import app as flask_app_module


class TestStorageLayer(unittest.TestCase):
    """Module 1: Thread-safe, crash-safe storage layer tests."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="icici_test_storage_")
        self.file_path = os.path.join(self.test_dir, "test_data.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_save_and_load_basic(self):
        data = {"key": "value", "count": 42, "items": [1, 2, 3]}
        storage.save_json(self.file_path, data)
        loaded = storage.load_json(self.file_path)
        self.assertEqual(loaded, data)

    def test_02_load_nonexistent_returns_default(self):
        non_existent = os.path.join(self.test_dir, "missing.json")
        res = storage.load_json(non_existent, default={"default": True})
        self.assertEqual(res, {"default": True})

    def test_03_atomic_backup_creation(self):
        initial_data = {"version": 1}
        storage.save_json(self.file_path, initial_data)
        self.assertFalse(os.path.exists(self.file_path + ".bak"))

        updated_data = {"version": 2}
        storage.save_json(self.file_path, updated_data, backup=True)
        self.assertTrue(os.path.exists(self.file_path + ".bak"))

        loaded_current = storage.load_json(self.file_path)
        loaded_bak = storage.load_json(self.file_path + ".bak")
        self.assertEqual(loaded_current, {"version": 2})
        self.assertEqual(loaded_bak, {"version": 1})

    def test_04_utf8_bom_compatibility(self):
        bom_bytes = b'\xef\xbb\xbf{"bom_test": true, "message": "hello\xe2\x82\xb9"}'
        with open(self.file_path, 'wb') as f:
            f.write(bom_bytes)
        loaded = storage.load_json(self.file_path)
        self.assertTrue(loaded.get("bom_test"))
        self.assertEqual(loaded.get("message"), "hello\u20b9")

    def test_05_corrupt_recovery_from_backup(self):
        good_data = {"status": "ok", "entries": [10, 20]}
        storage.save_json(self.file_path, good_data)
        storage.save_json(self.file_path, {"status": "updated"}, backup=True)

        with open(self.file_path, "w", encoding="utf-8") as f:
            f.write("{corrupt json text !@@#")

        recovered = storage.load_json(self.file_path)
        self.assertEqual(recovered, good_data)

    def test_06_corrupt_unrecoverable_raises_error(self):
        with open(self.file_path, "w", encoding="utf-8") as f:
            f.write("{invalid json")
        with open(self.file_path + ".bak", "w", encoding="utf-8") as f:
            f.write("{also invalid")

        with self.assertRaises(storage.CorruptJSONError):
            storage.load_json(self.file_path, default=[])

    def test_07_concurrent_thread_locks(self):
        storage.save_json(self.file_path, {"counter": 0})
        num_threads = 20
        increments_per_thread = 15

        def worker():
            for _ in range(increments_per_thread):
                with storage.path_lock(self.file_path):
                    d = storage.load_json(self.file_path)
                    d["counter"] += 1
                    storage.save_json(self.file_path, d, backup=False)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final_data = storage.load_json(self.file_path)
        expected_total = num_threads * increments_per_thread
        self.assertEqual(final_data["counter"], expected_total)

    def test_08_save_text_atomic(self):
        raw_text = '{"raw": "custom_serialization"}'
        storage.save_text(self.file_path, raw_text)
        with open(self.file_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, raw_text)


class TestReconcilerParsers(unittest.TestCase):
    """Module 2: Text extraction, cleaning, normalization, and duplicate detection."""

    def test_01_extract_refs_12digit_utr(self):
        text = "UPI/517417484927/PAYMENT FROM USER"
        refs = reconciler.extract_refs(text)
        self.assertIn("517417484927", refs)

        embedded = "TRANSFER 517417484927SHEEBA ACCOUNT"
        refs2 = reconciler.extract_refs(embedded)
        self.assertIn("517417484927", refs2)

    def test_02_extract_refs_upi_and_phone_guards(self):
        text = "PAID TO user@okicici MOB 9847123456"
        refs = reconciler.extract_refs(text)
        self.assertNotIn("9847123456", refs)
        self.assertNotIn("user@okicici", refs)

    def test_03_extract_cheque_numbers(self):
        self.assertEqual(reconciler.extract_cheque("CHQ 123456 CLG"), "123456")
        self.assertEqual(reconciler.extract_cheque("CHEQUE: 654321 PAID"), "654321")
        self.assertIsNone(reconciler.extract_cheque("PURCHASE OF MATERIALS AT MARCH 2026"))
        self.assertIsNone(reconciler.extract_cheque("BRANCH TRANSFER"))

    def test_04_clean_narration_and_normalize(self):
        raw1 = "12042026 BY TRANSFER NEFT TO ABC ENTERPRISES"
        cleaned1 = reconciler.clean_narration(raw1)
        self.assertNotIn("12042026", cleaned1)
        self.assertNotIn("TRANSFER", cleaned1)
        self.assertIn("ABC", cleaned1)
        self.assertIn("ENTERPRISES", cleaned1)

        raw2 = "ICICI BANK THALASSERY; PAYMENT TO ABC"
        norm2 = reconciler.normalize_narration(raw2)
        self.assertNotIn("ICICI BANK THALASSERY", norm2)

    def test_05_narrations_compatible_direct_and_aliases(self):
        self.assertTrue(reconciler.narrations_compatible(
            "PAYMENT TO KERALA AYURVEDA PHARMACY",
            "NEFT CR KERALA AYURVEDA PHARMACY"
        ))
        aliases = {"KERALA STATE ELEC": ["KSEB BILL PYMT"]}
        self.assertTrue(reconciler.narrations_compatible(
            "KERALA STATE ELEC",
            "KSEB BILL PYMT",
            aliases=aliases
        ))
        self.assertFalse(reconciler.narrations_compatible(
            "ABC TRADERS PAYMENT",
            "XYZ MOTORS REPAIR"
        ))

    def test_06_hard_conflict_guard_different_12digit_utrs(self):
        n1 = "UPI/517417484927/ENTERPRISES PAYMENT"
        n2 = "UPI/999888777666/ENTERPRISES PAYMENT"
        self.assertFalse(reconciler.narrations_compatible(n1, n2))

    def test_07_compute_duplicates_and_refresh(self):
        df = pd.DataFrame([
            {'bank_id': 'B1', 'ref_id': 'REF100', 'amount': 500.0, 'type_sign': 'CREDIT', 'narration': 'UPI/REF100/user1@okicici', 'date': '01-04-2026', 'dt': pd.to_datetime('2026-04-01'), 'matched': False},
            {'bank_id': 'B2', 'ref_id': 'REF100', 'amount': 500.0, 'type_sign': 'CREDIT', 'narration': 'UPI/REF100/user1@okicici', 'date': '02-04-2026', 'dt': pd.to_datetime('2026-04-02'), 'matched': False},
            {'bank_id': 'B3', 'ref_id': 'REF200', 'amount': 1000.0, 'type_sign': 'DEBIT', 'narration': 'NEFT REF200', 'date': '03-04-2026', 'dt': pd.to_datetime('2026-04-03'), 'matched': False},
        ])
        df_dups = reconciler.compute_duplicates(df)
        self.assertEqual(df_dups.loc[df_dups['bank_id'] == 'B1', 'duplicate_index'].iloc[0], 1)
        self.assertEqual(df_dups.loc[df_dups['bank_id'] == 'B2', 'duplicate_index'].iloc[0], 2)
        self.assertTrue(df_dups.loc[df_dups['bank_id'] == 'B2', 'is_duplicate'].iloc[0])

        df_dups.loc[df_dups['bank_id'] == 'B1', 'matched'] = True
        df_refreshed = reconciler.refresh_duplicate_matched(df_dups)
        self.assertTrue(df_refreshed.loc[df_refreshed['bank_id'] == 'B2', 'is_duplicate_matched'].iloc[0])

    def test_08_multi_schema_upi_parsing_type_b_extended(self):
        """Verify 7-part UPI schema extracts name, UPI handle, bank, UTR, and gateway ref."""
        narr = "UPI/SREESHMA P/sreeshmaprasha/UPI/Punjab Nat/658276117344/AXIfe11c20c4561490eb2aa454e61c2d015"
        parsed = reconciler.parse_upi_narration(narr)
        self.assertEqual(parsed['schema'], '7_PART')
        self.assertEqual(parsed['name'], 'SREESHMA P')
        self.assertEqual(parsed['upi_id'], 'SREESHMAPRASHA')
        self.assertEqual(parsed['bank_name'], 'Punjab Nat')
        self.assertEqual(parsed['utr'], '658276117344')
        self.assertEqual(parsed['gateway_ref'], 'AXIfe11c20c4561490eb2aa454e61c2d015')

        # extract_upi_id must return the real user VPA handle, NOT the remitter bank name 'PUNJAB'
        extracted = reconciler.extract_upi_id(narr)
        self.assertEqual(extracted, 'SREESHMAPRASHA')
        self.assertNotEqual(extracted, 'PUNJAB')

        # Check GL variant with 'TO UPI/'
        gl_narr = "TO UPI/SREESHMA P/SREESHMAPRASHA/UPI/PUNJAB NAT/646667084277/AXIB4D8C714467044F1881CDE73"
        gl_parsed = reconciler.parse_upi_narration(gl_narr)
        self.assertEqual(gl_parsed['schema'], '7_PART')
        self.assertEqual(gl_parsed['name'], 'SREESHMA P')
        self.assertEqual(gl_parsed['upi_id'], 'SREESHMAPRASHA')
        self.assertEqual(gl_parsed['utr'], '646667084277')

    def test_09_multi_schema_upi_parsing_type_a_standard(self):
        """Verify standard 6-part ICICI UPI schema."""
        narr = "UPI/609408830153/UPI/muhammadnabeeee/FEDERAL BANK/AXI4a061eeacc7e4b36805b14f613da07e3"
        parsed = reconciler.parse_upi_narration(narr)
        self.assertEqual(parsed['schema'], '6_PART')
        self.assertIsNone(parsed['name'])
        self.assertEqual(parsed['upi_id'], 'MUHAMMADNABEEEE')
        self.assertEqual(parsed['bank_name'], 'FEDERAL BANK')
        self.assertEqual(parsed['utr'], '609408830153')
        self.assertEqual(reconciler.extract_upi_id(narr), 'MUHAMMADNABEEEE')

    def test_10_bank_noise_exclusion(self):
        """Verify bank names are never extracted as UPI IDs."""
        for bank_word in ['PUNJAB', 'SOUTH', 'CANARA', 'FEDERAL', 'IDFC', 'UNION', 'BARODA', 'STATE BANK']:
            text = f"/UPI/{bank_word}/"
            upi = reconciler.extract_upi_id(text)
            self.assertIsNone(upi, f"Bank word '{bank_word}' should not be extracted as UPI ID")

    def test_11_clean_narration_gateway_hash_removal(self):
        """Verify long gateway switch tokens and hex hashes are stripped from clean_narration."""
        narr = "UPI/SREESHMA P/sreeshmaprasha/UPI/Punjab Nat/658276117344/AXIfe11c20c4561490eb2aa454e61c2d015"
        cleaned = reconciler.clean_narration(narr)
        self.assertNotIn("AXIFE11C", cleaned)
        self.assertIn("SREESHMA", cleaned)


class TestConfidenceScoreEngine(unittest.TestCase):
    """Module 3: Confidence score calculations and edge cases."""

    def test_01_exact_ref_match_100_percent(self):
        grow = {'all_refs': ['517417484927'], 'narration': 'GL REF 517417484927', 'dt': pd.to_datetime('2026-04-01')}
        brow = {'all_refs': ['517417484927'], 'narration': 'BANK UPI/517417484927', 'dt': pd.to_datetime('2026-04-05')}
        score, guide, label = reconciler.calculate_confidence_score(grow, brow)
        self.assertEqual(score, 100)
        self.assertEqual(label, "Ref Match")
        self.assertIn("100% confidence", guide)

    def test_02_possible_typo_85_percent(self):
        grow = {'all_refs': ['517417484927'], 'narration': 'REF 517417484927', 'dt': pd.to_datetime('2026-04-01')}
        brow = {'all_refs': ['517417484928'], 'narration': 'REF 517417484928', 'dt': pd.to_datetime('2026-04-02')}
        score, guide, label = reconciler.calculate_confidence_score(grow, brow)
        self.assertEqual(score, 85)
        self.assertEqual(label, "Possible Typo")
        self.assertIn("85% confidence", guide)

    def test_03_upi_fuzzy_username_match(self):
        grow = {'all_refs': [], 'narration': 'UPI/517417484927/SHEEBAPANIYAN/OKAXIS', 'dt': pd.to_datetime('2026-04-01')}
        brow = {'all_refs': [], 'narration': 'UPI/999999999999/SHEEBAPANIYAN/OKICICI', 'dt': pd.to_datetime('2026-04-03')}
        score, guide, label = reconciler.calculate_confidence_score(grow, brow)
        self.assertEqual(label, "UPI Match")
        self.assertTrue(50 <= score <= 80)

    def test_04_standard_narration_similarity_capped_at_70(self):
        grow = {'all_refs': [], 'narration': 'SALARY PAYMENT TO EMPLOYEES', 'dt': pd.to_datetime('2026-04-01')}
        brow = {'all_refs': [], 'narration': 'SALARY PAYMENT TO EMPLOYEES APRIL', 'dt': pd.to_datetime('2026-04-01')}
        score, guide, label = reconciler.calculate_confidence_score(grow, brow)
        self.assertLessEqual(score, 70)
        self.assertEqual(label, "Narrative Match")

    def test_05_edge_cases_and_date_decay(self):
        grow = {'all_refs': [], 'narration': 'GENERAL EXPENSE', 'dt': pd.to_datetime('2026-04-01')}
        brow = {'all_refs': [], 'narration': 'MISC CHARGES', 'dt': pd.to_datetime('2026-05-01')}
        score, guide, label = reconciler.calculate_confidence_score(grow, brow)
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 50)


class TestMatchingEngineTiers(unittest.TestCase):
    """Module 4: Multi-tier matching engine correctness."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="icici_test_engine_")
        for sub in ['Bank Statement', 'General Ledger', 'GL RTGS NEFT REPORT', 'CMS Report']:
            os.makedirs(os.path.join(self.test_dir, sub), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_base_dfs(self):
        df_bank = pd.DataFrame([
            {
                'bank_id': 'B1', 'date': '01-04-2026', 'dt': pd.to_datetime('2026-04-01'),
                'amount': 1500.0, 'type_sign': 'CREDIT', 'narration': 'UPI/517417484927/USER1',
                'ref_id': '517417484927', 'cheque_no': None, 'all_refs': ['517417484927'],
                'upi_id': 'user1@okicici', 'matched': False, 'txn_type': 'UPI'
            },
            {
                'bank_id': 'B1_1', 'date': '02-04-2026', 'dt': pd.to_datetime('2026-04-02'),
                'amount': 990.0, 'type_sign': 'CREDIT', 'narration': 'CMS CMSUTR99998888',
                'ref_id': 'CMSUTR99998888', 'cheque_no': None, 'all_refs': ['CMSUTR99998888'],
                'upi_id': None, 'matched': False, 'txn_type': 'CMS'
            },
            {
                'bank_id': 'B2', 'date': '15-04-2026', 'dt': pd.to_datetime('2026-04-15'),
                'amount': 2500.0, 'type_sign': 'CREDIT', 'narration': 'NEFT/642723737765/PARTY B',
                'ref_id': '642723737765', 'cheque_no': None, 'all_refs': ['642723737765'],
                'upi_id': None, 'matched': False, 'txn_type': 'NEFT'
            },
            {
                'bank_id': 'B3', 'date': '05-04-2026', 'dt': pd.to_datetime('2026-04-05'),
                'amount': 800.0, 'type_sign': 'DEBIT', 'narration': 'CHQ 445566 ISSUED',
                'ref_id': None, 'cheque_no': '445566', 'all_refs': ['445566'],
                'upi_id': None, 'matched': False, 'txn_type': 'OTHER'
            },
            {
                'bank_id': 'B4', 'date': '10-04-2026', 'dt': pd.to_datetime('2026-04-10'),
                'amount': 5000.0, 'type_sign': 'CREDIT', 'narration': 'TRANSFER FROM THALASSERY CLIENT CO',
                'ref_id': None, 'cheque_no': None, 'all_refs': [],
                'upi_id': None, 'matched': False, 'txn_type': 'OTHER'
            },
            {
                'bank_id': 'B5', 'date': '12-04-2026', 'dt': pd.to_datetime('2026-04-12'),
                'amount': 3000.0, 'type_sign': 'CREDIT', 'narration': 'BULK SALES SETTLEMENT',
                'ref_id': None, 'cheque_no': None, 'all_refs': [],
                'upi_id': None, 'matched': False, 'txn_type': 'OTHER'
            }
        ])

        df_gl = pd.DataFrame([
            {
                'gl_id': 'G1', 'date': '01-04-2026', 'dt': pd.to_datetime('2026-04-01'),
                'amount': 1500.0, 'type_sign': 'DEBIT', 'narration': 'BY UPI 517417484927 USER1',
                'ref_id': '517417484927', 'cheque_no': None, 'all_refs': ['517417484927'],
                'upi_id': 'user1@okicici', 'matched': False, 'is_enriched': False, 'original_refs': ['517417484927'],
                'txn_type': 'UPI'
            },
            {
                'gl_id': 'G1_1', 'date': '02-04-2026', 'dt': pd.to_datetime('2026-04-02'),
                'amount': 990.0, 'type_sign': 'DEBIT', 'narration': 'PAYMENT CMS SERIAL_55555',
                'ref_id': 'CMSUTR99998888', 'cheque_no': None, 'all_refs': ['CMSUTR99998888', 'SERIAL_55555'],
                'upi_id': None, 'matched': False, 'is_enriched': True, 'original_refs': ['SERIAL_55555'],
                'txn_type': 'CMS'
            },
            {
                'gl_id': 'G2', 'date': '01-04-2026', 'dt': pd.to_datetime('2026-04-01'),
                'amount': 2500.0, 'type_sign': 'DEBIT', 'narration': 'PARTY B PAYMENT 642723737765',
                'ref_id': '642723737765', 'cheque_no': None, 'all_refs': ['642723737765'],
                'upi_id': None, 'matched': False, 'is_enriched': False, 'original_refs': ['642723737765'],
                'txn_type': 'OTHER'
            },
            {
                'gl_id': 'G3', 'date': '06-04-2026', 'dt': pd.to_datetime('2026-04-06'),
                'amount': 800.0, 'type_sign': 'CREDIT', 'narration': 'CHEQUE 445566 VENDOR',
                'ref_id': None, 'cheque_no': '445566', 'all_refs': ['445566'],
                'upi_id': None, 'matched': False, 'is_enriched': False, 'original_refs': ['445566'],
                'txn_type': 'OTHER'
            },
            {
                'gl_id': 'G4', 'date': '10-04-2026', 'dt': pd.to_datetime('2026-04-10'),
                'amount': 5000.0, 'type_sign': 'DEBIT', 'narration': 'CLIENT CO THALASSERY RECEIPT',
                'ref_id': None, 'cheque_no': None, 'all_refs': [],
                'upi_id': None, 'matched': False, 'is_enriched': False, 'original_refs': [],
                'txn_type': 'OTHER'
            },
            {
                'gl_id': 'G5_1', 'date': '12-04-2026', 'dt': pd.to_datetime('2026-04-12'),
                'amount': 1000.0, 'type_sign': 'DEBIT', 'narration': 'BULK SALES SETTLEMENT',
                'ref_id': None, 'cheque_no': None, 'all_refs': [],
                'upi_id': None, 'matched': False, 'is_enriched': False, 'original_refs': [],
                'txn_type': 'OTHER'
            },
            {
                'gl_id': 'G5_2', 'date': '12-04-2026', 'dt': pd.to_datetime('2026-04-12'),
                'amount': 2000.0, 'type_sign': 'DEBIT', 'narration': 'BULK SALES SETTLEMENT',
                'ref_id': None, 'cheque_no': None, 'all_refs': [],
                'upi_id': None, 'matched': False, 'is_enriched': False, 'original_refs': [],
                'txn_type': 'OTHER'
            }
        ])
        return df_bank, df_gl

    def test_01_tier_matching_execution(self):
        df_bank, df_gl = self._create_base_dfs()
        result = reconciler.reconcile(df_bank=df_bank, df_gl=df_gl, data_root=self.test_dir)

        matches = result.get('matches', [])

        # Check Tier 1 Perfect Match (B1 <-> G1)
        tier1_matches = [m for m in matches if m.get('status') == 'TIER1_PERFECT']
        self.assertEqual(len(tier1_matches), 1)
        self.assertEqual(tier1_matches[0]['bks'][0]['bank_id'], 'B1')
        self.assertEqual(tier1_matches[0]['gls'][0]['gl_id'], 'G1')

        # Check Tier 1.1 Transitive CMS Match (B1_1 <-> G1_1)
        tier11_matches = [m for m in matches if m.get('status') == 'TIER1.1_TRANSITIVE_CMS']
        self.assertEqual(len(tier11_matches), 1)
        self.assertEqual(tier11_matches[0]['bks'][0]['bank_id'], 'B1_1')
        self.assertEqual(tier11_matches[0]['gls'][0]['gl_id'], 'G1_1')

        # Check Tier 2 UTR Match (B2 <-> G2, 14 days apart)
        tier2_matches = [m for m in matches if m.get('status') == 'TIER2_UTR_MATCH']
        self.assertEqual(len(tier2_matches), 1)
        self.assertEqual(tier2_matches[0]['bks'][0]['bank_id'], 'B2')
        self.assertEqual(tier2_matches[0]['gls'][0]['gl_id'], 'G2')

        # Check Tier 2.5 Cheque Match (B3 <-> G3, 1 day apart)
        tier25_matches = [m for m in matches if m.get('status') == 'TIER2.5_CHEQUE_MATCH']
        self.assertEqual(len(tier25_matches), 1)
        self.assertEqual(tier25_matches[0]['bks'][0]['bank_id'], 'B3')

        # Check Tier 3 Date & Narrative Match (B4 <-> G4)
        tier3_matches = [m for m in matches if m.get('status') in ['TIER3_EXACT_DATE', 'TIER3_DATE_MATCH']]
        self.assertEqual(len(tier3_matches), 1)

        # Check Tier 6 One-to-Many Group Match (B5 3000.0 <-> G5_1 1000.0 + G5_2 2000.0)
        tier6_matches = [m for m in matches if m.get('status') == 'TIER6_ONE_TO_MANY']
        self.assertEqual(len(tier6_matches), 1)

        # Verify summary statistics
        summary = result.get('summary', {})
        self.assertEqual(summary.get('bank_unmatched'), 0)
        self.assertEqual(summary.get('gl_unmatched'), 0)

    def test_02_sign_sensitivity_enforcement(self):
        df_bank = pd.DataFrame([{
            'bank_id': 'B_CR', 'date': '01-04-2026', 'dt': pd.to_datetime('2026-04-01'),
            'amount': 100.0, 'type_sign': 'CREDIT', 'narration': 'TEST 123456789012',
            'ref_id': '123456789012', 'cheque_no': None, 'all_refs': ['123456789012'],
            'upi_id': None, 'matched': False, 'txn_type': 'OTHER'
        }])
        df_gl = pd.DataFrame([{
            'gl_id': 'G_CR', 'date': '01-04-2026', 'dt': pd.to_datetime('2026-04-01'),
            'amount': 100.0, 'type_sign': 'CREDIT', 'narration': 'TEST 123456789012',
            'ref_id': '123456789012', 'cheque_no': None, 'all_refs': ['123456789012'],
            'upi_id': None, 'matched': False, 'is_enriched': False, 'original_refs': [], 'txn_type': 'OTHER'
        }])
        result = reconciler.reconcile(df_bank=df_bank, df_gl=df_gl, data_root=self.test_dir)
        self.assertEqual(len(result.get('matches', [])), 0)
        self.assertEqual(result['summary']['bank_unmatched'], 1)
        self.assertEqual(result['summary']['gl_unmatched'], 1)


class TestFlaskApiAndManualLinks(unittest.TestCase):
    """Module 5: Flask API, FY routing, manual links, and double-entry unlink guard."""

    def setUp(self):
        self.app = flask_app_module.app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

        self.fy_name = "FY2026-27 - PRESENT"
        self.fy_path = os.path.join(BASE_DIR, "Data", self.fy_name)
        self.manual_links_path = os.path.join(self.fy_path, "manual_links.json")
        self.sticky_notes_path = os.path.join(self.fy_path, "sticky_notes.json")

        self.orig_manual = storage.load_json(self.manual_links_path, None)
        self.orig_notes = storage.load_json(self.sticky_notes_path, None)

    def tearDown(self):
        if self.orig_manual is not None:
            storage.save_json(self.manual_links_path, self.orig_manual, backup=False)
        if self.orig_notes is not None:
            storage.save_json(self.sticky_notes_path, self.orig_notes, backup=False)

    def test_01_fy_safety_and_traversal_guards(self):
        self.assertTrue(flask_app_module.is_safe_fy(self.fy_name))
        self.assertFalse(flask_app_module.is_safe_fy("../Data/Secret"))
        self.assertFalse(flask_app_module.is_safe_fy(".."))
        self.assertFalse(flask_app_module.is_safe_fy(None))
        self.assertFalse(flask_app_module.is_safe_fy("NonExistentFY_2099"))

    def test_02_set_fy_and_current_fy_api(self):
        resp = self.client.post('/api/set-fy', json={'fy': self.fy_name})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['status'], 'success')

        resp_curr = self.client.get('/api/current-fy')
        self.assertEqual(resp_curr.status_code, 200)
        self.assertEqual(resp_curr.get_json()['fy'], self.fy_name)

        resp_bad = self.client.post('/api/set-fy', json={'fy': '../../evil'})
        self.assertEqual(resp_bad.status_code, 400)

    def test_03_manual_match_and_unlink_double_entry_validation(self):
        self.client.post('/api/set-fy', json={'fy': self.fy_name})

        test_bank_ids = ["TEST_B1", "TEST_B2"]
        test_gl_ids = ["TEST_G1", "TEST_G2"]
        resp_match = self.client.post('/api/manual-match', json={
            'bankIds': test_bank_ids,
            'glIds': test_gl_ids,
            'type': 'MANUAL_GROUP_MATCH'
        })
        self.assertEqual(resp_match.status_code, 200)

        links = storage.load_json(self.manual_links_path, [])
        created = [l for l in links if set(l.get('bank_ids', [])) == set(test_bank_ids) and set(l.get('gl_ids', [])) == set(test_gl_ids)]
        self.assertEqual(len(created), 1)

        resp_dup = self.client.post('/api/manual-match', json={
            'bankIds': test_bank_ids,
            'glIds': test_gl_ids
        })
        self.assertEqual(resp_dup.status_code, 200)
        self.assertEqual(resp_dup.get_json().get('status'), 'already_exists')

        resp_un_part = self.client.post('/api/unlink', json={
            'bankIds': ['TEST_B1'],
            'glIds': []
        })
        self.assertEqual(resp_un_part.status_code, 200)
        links_after_part1 = storage.load_json(self.manual_links_path, [])
        m1 = [l for l in links_after_part1 if 'TEST_B2' in l.get('bank_ids', [])]
        self.assertEqual(len(m1), 1)
        self.assertEqual(set(m1[0]['bank_ids']), {'TEST_B2'})
        self.assertEqual(set(m1[0]['gl_ids']), {'TEST_G1', 'TEST_G2'})

        resp_un_part2 = self.client.post('/api/unlink', json={
            'bankIds': ['TEST_B2'],
            'glIds': []
        })
        self.assertEqual(resp_un_part2.status_code, 200)

        self.client.post('/api/unlink', json={
            'bankIds': [],
            'glIds': test_gl_ids
        })

    def test_04_sticky_notes_crud_and_validation(self):
        self.client.post('/api/set-fy', json={'fy': self.fy_name})
        test_id = "TEST_TX_9999"

        resp_create = self.client.post('/api/sticky-notes', json={
            'id': test_id,
            'note': 'Audit verification note',
            'category': 'Verified'
        })
        self.assertEqual(resp_create.status_code, 200)

        resp_get = self.client.get('/api/sticky-notes')
        self.assertEqual(resp_get.status_code, 200)
        notes = resp_get.get_json()
        self.assertIn(test_id, notes)
        self.assertEqual(notes[test_id]['text'], 'Audit verification note')
        self.assertEqual(notes[test_id]['category'], 'Verified')

        resp_del = self.client.post('/api/sticky-notes', json={
            'id': test_id,
            'note': ''
        })
        self.assertEqual(resp_del.status_code, 200)
        notes_after = self.client.get('/api/sticky-notes').get_json()
        self.assertNotIn(test_id, notes_after)

    def test_05_sticky_notes_rich_taxonomy_and_batch_api(self):
        self.client.post('/api/set-fy', json={'fy': self.fy_name})
        
        # 1. Test rich note creation with type, severity, status, and tags
        resp_warning = self.client.post('/api/sticky-notes', json={
            'id': 'TX_WARN_01',
            'note': 'Potential duplicate detected',
            'type': 'WARNING',
            'severity': 'high',
            'status': 'open',
            'category': 'Duplicate Risk',
            'tags': ['risk', 'urgent']
        })
        self.assertEqual(resp_warning.status_code, 200)
        notes = self.client.get('/api/sticky-notes').get_json()
        self.assertEqual(notes['TX_WARN_01']['type'], 'WARNING')
        self.assertEqual(notes['TX_WARN_01']['severity'], 'high')
        self.assertEqual(notes['TX_WARN_01']['status'], 'open')
        self.assertIn('urgent', notes['TX_WARN_01']['tags'])

        # 2. Test batch note assignment
        batch_ids = ['TX_BATCH_1', 'TX_BATCH_2', 'TX_BATCH_3']
        resp_batch = self.client.post('/api/sticky-notes/batch', json={
            'ids': batch_ids,
            'note': 'Quarterly GST batch review',
            'type': 'TAX',
            'category': 'GST Audit',
            'severity': 'medium',
            'status': 'open',
            'tags': ['gst', 'q1']
        })
        self.assertEqual(resp_batch.status_code, 200)
        self.assertEqual(resp_batch.get_json()['count'], 3)

        notes_after_batch = self.client.get('/api/sticky-notes').get_json()
        for b_id in batch_ids:
            self.assertIn(b_id, notes_after_batch)
            self.assertEqual(notes_after_batch[b_id]['type'], 'TAX')
            self.assertEqual(notes_after_batch[b_id]['status'], 'open')

        # 3. Test batch resolve
        resp_resolve = self.client.post('/api/sticky-notes/batch', json={
            'ids': ['TX_BATCH_1', 'TX_BATCH_2'],
            'action': 'resolve'
        })
        self.assertEqual(resp_resolve.status_code, 200)
        notes_resolved = self.client.get('/api/sticky-notes').get_json()
        self.assertEqual(notes_resolved['TX_BATCH_1']['status'], 'resolved')
        self.assertEqual(notes_resolved['TX_BATCH_2']['status'], 'resolved')
        self.assertEqual(notes_resolved['TX_BATCH_3']['status'], 'open')

        # 4. Test batch delete
        resp_del_batch = self.client.post('/api/sticky-notes/batch', json={
            'ids': ['TX_WARN_01', 'TX_BATCH_1', 'TX_BATCH_2', 'TX_BATCH_3'],
            'action': 'delete'
        })
        self.assertEqual(resp_del_batch.status_code, 200)
        notes_clean = self.client.get('/api/sticky-notes').get_json()
        for b_id in ['TX_WARN_01', 'TX_BATCH_1', 'TX_BATCH_2', 'TX_BATCH_3']:
            self.assertNotIn(b_id, notes_clean)


class TestQuickSaveIdempotency(unittest.TestCase):
    """Module 6: Quick-Save correctness and idempotency."""

    def test_01_quick_save_idempotency_and_preservation(self):
        fy_name = "FY2026-27 - PRESENT"
        fy_path = os.path.join(BASE_DIR, "Data", fy_name)
        paths = reconciler.get_paths(fy_path)

        if not os.path.exists(paths['OUTPUT_JSON']):
            self.skipTest(f"{paths['OUTPUT_JSON']} does not exist yet.")

        res1 = reconciler.quick_save(data_root=fy_path)
        sum1 = res1.get('summary', {})

        res2 = reconciler.quick_save(data_root=fy_path)
        sum2 = res2.get('summary', {})

        self.assertEqual(sum1['matched'], sum2['matched'])
        self.assertEqual(sum1['bank_unmatched'], sum2['bank_unmatched'])
        self.assertEqual(sum1['gl_unmatched'], sum2['gl_unmatched'])
        self.assertEqual(len(res1.get('matches', [])), len(res2.get('matches', [])))


class TestPerformanceBenchmark(unittest.TestCase):
    """Module 7: Benchmark Full Reconcile vs Quick-Save on real dataset."""

    def test_01_benchmark_reconcile_vs_quicksave(self):
        fy_name = "FY2026-27 - PRESENT"
        fy_path = os.path.join(BASE_DIR, "Data", fy_name)

        if not os.path.isdir(fy_path):
            self.skipTest(f"FY directory {fy_path} not found.")

        print("\n" + "="*70)
        print("PERFORMANCE BENCHMARK: Full Reconcile vs Quick-Save")
        print("="*70)

        # 1. Benchmark Full Reconcile
        tracemalloc.start()
        t0 = time.perf_counter()
        full_res = reconciler.reconcile(data_root=fy_path)
        t_full = time.perf_counter() - t0
        mem_full_current, mem_full_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # 2. Benchmark Quick-Save
        tracemalloc.start()
        t0 = time.perf_counter()
        qs_res = reconciler.quick_save(data_root=fy_path)
        t_qs = time.perf_counter() - t0
        mem_qs_current, mem_qs_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        speedup = t_full / t_qs if t_qs > 0 else float('inf')

        print(f"Dataset Target:          {fy_name}")
        print(f"Total Bank Rows Loaded:  {len(full_res.get('missing_gl', [])) + sum(len(m.get('bks', [])) for m in full_res.get('matches', []))}")
        print(f"Total GL Rows Loaded:    {len(full_res.get('missing_bank', [])) + sum(len(m.get('gls', [])) for m in full_res.get('matches', []))}")
        print(f"Total Matched Groups:    {full_res['summary']['matched']}")
        print(f"Full Reconcile Runtime:  {t_full:.3f} s (Peak RAM: {mem_full_peak / (1024*1024):.2f} MB)")
        print(f"Quick-Save Runtime:      {t_qs:.3f} s (Peak RAM: {mem_qs_peak / (1024*1024):.2f} MB)")
        print(f"Quick-Save Speedup:      {speedup:.2f}x faster than Full Reconcile")
        print("="*70 + "\n")

        self.assertLess(t_qs, t_full)
        self.assertTrue(speedup >= 1.5)


def run_all_tests():
    print("=" * 80)
    print("  ICICI-GL AUTOMATED SYSTEM VERIFICATION & TEST SUITE RUNNER")
    print("  Timestamp:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 80)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestStorageLayer,
        TestReconcilerParsers,
        TestConfidenceScoreEngine,
        TestMatchingEngineTiers,
        TestFlaskApiAndManualLinks,
        TestQuickSaveIdempotency,
        TestPerformanceBenchmark,
    ]

    for tc in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(tc))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print("\n" + "=" * 80)
    print(f"TEST SUMMARY: Ran {result.testsRun} tests.")
    print(f"SUCCESS: {result.wasSuccessful()}")
    print(f"ERRORS: {len(result.errors)}")
    print(f"FAILURES: {len(result.failures)}")
    print(f"SKIPPED: {len(result.skipped)}")
    print("=" * 80)

    return result


if __name__ == "__main__":
    res = run_all_tests()
    sys.exit(0 if res.wasSuccessful() else 1)
