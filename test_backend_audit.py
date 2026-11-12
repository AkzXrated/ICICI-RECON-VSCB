import os
import sys
import json
import gzip
import time
import shutil
import tempfile
import threading
import unittest
from io import BytesIO

import storage
import app
import reconciler

class TestStorageAuditing(unittest.TestCase):
    def setUp(self):
        # Create temp dir on D: if possible or default temp
        try:
            self.test_dir = tempfile.mkdtemp(prefix="icici_audit_storage_", dir=app.BASE_DIR)
        except Exception:
            self.test_dir = tempfile.mkdtemp(prefix="icici_audit_storage_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ---------------------------------------------------------
    # 1. Concurrency and File Locking
    # ---------------------------------------------------------
    def test_path_lock_reentrancy_and_normalization(self):
        """Verify path_lock is reentrant and normalizes paths (case, slashes, relative)."""
        p1 = os.path.join(self.test_dir, "test_file.json")
        p2 = os.path.join(self.test_dir, "TEST_FILE.JSON")
        p3 = os.path.join(self.test_dir, "sub", "..", "test_file.json")
        p4 = p1.replace("\\", "/")

        lock1 = storage.path_lock(p1)
        lock2 = storage.path_lock(p2)
        lock3 = storage.path_lock(p3)
        lock4 = storage.path_lock(p4)

        # On Windows (case-insensitive filesystem), normcase normalizes case and slashes
        self.assertIs(lock1, lock2, "Locks for case-differing paths should be identical")
        self.assertIs(lock1, lock3, "Locks for redundant-segment paths should be identical")
        self.assertIs(lock1, lock4, "Locks for forward vs backward slash paths should be identical")

        # Test reentrancy (same thread acquiring lock multiple times)
        with lock1:
            with lock1:
                with lock2:
                    with lock3:
                        self.assertTrue(True, "Nested reentrant acquisition succeeded")

    def test_concurrent_read_modify_write_stress(self):
        """Verify high concurrency read-modify-write causes zero lost updates or file corruptions."""
        p = os.path.join(self.test_dir, "counter.json")
        storage.save_json(p, {"counter": 0, "log": []})

        num_threads = 16
        ops_per_thread = 25
        errors = []

        def worker(thread_id):
            try:
                for i in range(ops_per_thread):
                    with storage.path_lock(p):
                        data = storage.load_json(p)
                        data["counter"] += 1
                        data["log"].append(f"{thread_id}-{i}")
                        storage.save_json(p, data, backup=False)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Thread errors encountered: {errors}")
        final_data = storage.load_json(p)
        expected_count = num_threads * ops_per_thread
        self.assertEqual(final_data["counter"], expected_count)
        self.assertEqual(len(final_data["log"]), expected_count)

    # ---------------------------------------------------------
    # 2. Atomic JSON Write & Recovery
    # ---------------------------------------------------------
    def test_atomic_write_creates_and_cleans_tmp(self):
        """Verify _atomic_write writes through .tmp and cleans it up."""
        p = os.path.join(self.test_dir, "atomic.json")
        data = {"hello": "world"}
        storage.save_json(p, data, backup=False)

        self.assertTrue(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".tmp"), "Temp file must not remain after successful write")
        self.assertEqual(storage.load_json(p), data)

    def test_backup_creation_and_rotation(self):
        """Verify .bak creation preserves the previous good version."""
        p = os.path.join(self.test_dir, "data_versioned.json")
        v1 = {"ver": 1, "records": ["A", "B"]}
        storage.save_json(p, v1, backup=True)
        self.assertFalse(os.path.exists(p + ".bak"), "No .bak on initial creation")

        v2 = {"ver": 2, "records": ["A", "B", "C"]}
        storage.save_json(p, v2, backup=True)
        self.assertTrue(os.path.exists(p + ".bak"))
        with open(p + ".bak", 'r', encoding='utf-8') as f:
            self.assertEqual(json.load(f), v1)

        v3 = {"ver": 3, "records": ["A", "B", "C", "D"]}
        storage.save_json(p, v3, backup=True)
        with open(p + ".bak", 'r', encoding='utf-8') as f:
            self.assertEqual(json.load(f), v2)

    def test_corrupt_file_recovery(self):
        """Verify load_json recovers from .bak when primary file is corrupt."""
        p = os.path.join(self.test_dir, "corrupt_test.json")
        good_data = {"important": "matches", "items": [1, 2, 3]}
        storage.save_json(p, good_data, backup=True)
        storage.save_json(p, {"important": "matches_v2", "items": [1, 2, 3, 4]}, backup=True)

        # Corrupt primary file
        with open(p, 'wb') as f:
            f.write(b"{\nincomplete corrupt json...")

        # load_json must recover from .bak
        recovered = storage.load_json(p)
        self.assertEqual(recovered, good_data)

    def test_corrupt_file_without_bak_raises_and_preserves(self):
        """Verify CorruptJSONError is raised and default is NOT returned for broken file."""
        p = os.path.join(self.test_dir, "unrecoverable.json")
        with open(p, 'w', encoding='utf-8') as f:
            f.write("GARBAGE_NOT_JSON")

        with self.assertRaises(storage.CorruptJSONError):
            storage.load_json(p, default=[])

        # Verify file was not wiped
        with open(p, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), "GARBAGE_NOT_JSON")

    def test_corrupt_non_utf8_bytes_recovery(self):
        """Verify load_json recovers from .bak when primary file has invalid non-UTF-8 bytes."""
        p = os.path.join(self.test_dir, "non_utf8.json")
        good = {"status": "intact", "code": 200}
        storage.save_json(p, good, backup=True)
        storage.save_json(p, {"status": "v2"}, backup=True)

        # Corrupt primary with invalid UTF-8 sequence
        with open(p, 'wb') as f:
            f.write(b'\x80\x81\xFF\xFE\xAA\xBB')

        recovered = storage.load_json(p)
        self.assertEqual(recovered, good)

    def test_save_text_atomic(self):
        """Verify save_text writes atomically and removes .tmp."""
        p = os.path.join(self.test_dir, "raw.txt")
        storage.save_text(p, "PRE_SERIALIZED_PAYLOAD")
        self.assertTrue(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".tmp"))
        with open(p, 'r', encoding='utf-8') as f:
            self.assertEqual(f.read(), "PRE_SERIALIZED_PAYLOAD")

    def test_no_file_descriptor_leak(self):
        """Verify repeated load_json / save_json does not leak file descriptors."""
        p = os.path.join(self.test_dir, "leak_test.json")
        storage.save_json(p, {"count": 0})
        for i in range(250):
            data = storage.load_json(p)
            data["count"] += 1
            storage.save_json(p, data, backup=True)
        final = storage.load_json(p)
        self.assertEqual(final["count"], 250)

    def test_utf8_bom_handling(self):
        """Verify files with UTF-8 BOM are loaded seamlessly."""
        p = os.path.join(self.test_dir, "bom_test.json")
        obj = {"currency": "INR", "symbols": "₹ € $"}
        with open(p, 'wb') as f:
            f.write(b'\xef\xbb\xbf' + json.dumps(obj).encode('utf-8'))

        loaded = storage.load_json(p)
        self.assertEqual(loaded, obj)

class TestAppIntegrityAndSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Set up a sandbox dummy FY for full API testing
        cls.sandbox_fy = "FY2099-00 - TEST"
        cls.sandbox_path = os.path.join(app.DATA_ROOT_DIR, cls.sandbox_fy)
        cls.bank_dir = os.path.join(cls.sandbox_path, "Bank Statement")
        cls.gl_dir = os.path.join(cls.sandbox_path, "General Ledger")
        os.makedirs(cls.bank_dir, exist_ok=True)
        os.makedirs(cls.gl_dir, exist_ok=True)

        # Create dummy source files for sandbox FY (with 7 header lines for PSV skiprows=7)
        cls.bank_psv = os.path.join(cls.bank_dir, "OPR.psv")
        with open(cls.bank_psv, 'w', encoding='utf-8') as f:
            f.write("Line 1 Header\nLine 2 Header\nLine 3 Header\nLine 4 Header\nLine 5 Header\nLine 6 Header\nLine 7 Header\n")
            f.write("Transaction ID|Value Date|Txn Posted Date|ChequeNo|Description|Cr/Dr|Transaction Amount(INR)|Available Balance(INR)\n")
            f.write("101|01/04/2026|01/04/2026||PAYMENT RECEIVED FROM ABC|CR|5000.00|105000.00\n")
            f.write("102|02/04/2026|02/04/2026||SALARY VENDOR XYZ|DR|2000.00|103000.00\n")
            f.write("103|03/04/2026|03/04/2026||CLIENT PAYMENT 103|CR|1500.00|104500.00\n")
            f.write("104|03/04/2026|03/04/2026||CLIENT PAYMENT 104|CR|1500.00|106000.00\n")

        cls.gl_xml = os.path.join(cls.gl_dir, "GL.xml")
        with open(cls.gl_xml, 'w', encoding='utf-8') as f:
            f.write("""<ENVELOPE>
  <G_DAY_DT>
    <DAY_DT>2026-04-01</DAY_DT>
    <G_PARTI>
      <PARTI>Payment from ABC</PARTI>
      <DEBIT>0</DEBIT>
      <CREDIT>5000.00</CREDIT>
    </G_PARTI>
    <G_PARTI>
      <PARTI>Vendor XYZ Salary</PARTI>
      <DEBIT>2000.00</DEBIT>
      <CREDIT>0</CREDIT>
    </G_PARTI>
    <G_PARTI>
      <PARTI>Client Payment 103</PARTI>
      <DEBIT>0</DEBIT>
      <CREDIT>1500.00</CREDIT>
    </G_PARTI>
    <G_PARTI>
      <PARTI>Client Payment 104</PARTI>
      <DEBIT>0</DEBIT>
      <CREDIT>1500.00</CREDIT>
    </G_PARTI>
    <G_PARTI>
      <PARTI>Internal Transfer Alpha</PARTI>
      <DEBIT>100.00</DEBIT>
      <CREDIT>0</CREDIT>
    </G_PARTI>
    <G_PARTI>
      <PARTI>Internal Transfer Beta</PARTI>
      <DEBIT>0</DEBIT>
      <CREDIT>100.00</CREDIT>
    </G_PARTI>
    <G_PARTI>
      <PARTI>Internal Transfer Gamma</PARTI>
      <DEBIT>0</DEBIT>
      <CREDIT>100.00</CREDIT>
    </G_PARTI>
  </G_DAY_DT>
</ENVELOPE>""")

        cls.manual_links_path = os.path.join(cls.sandbox_path, "manual_links.json")
        cls.sticky_notes_path = os.path.join(cls.sandbox_path, "sticky_notes.json")
        cls.data_cache_path = os.path.join(cls.sandbox_path, "data.json")

    @classmethod
    def tearDownClass(cls):
        # Remove sandbox directory
        shutil.rmtree(cls.sandbox_path, ignore_errors=True)

    def setUp(self):
        app.app.testing = True
        self.client = app.app.test_client()
        # Reset sandbox files before each test
        storage.save_json(self.manual_links_path, [], backup=False)
        storage.save_json(self.sticky_notes_path, {}, backup=False)
        if os.path.exists(self.data_cache_path):
            try: os.remove(self.data_cache_path)
            except OSError: pass
        if self.sandbox_fy in app.CACHE:
            del app.CACHE[self.sandbox_fy]

    # ---------------------------------------------------------
    # 3. API Security & Validation
    # ---------------------------------------------------------
    def test_fy_traversal_defense(self):
        """Verify path traversal attacks are rejected across all FY functions and endpoints."""
        attacks = [
            "..",
            "../",
            "..\\",
            "../../etc/passwd",
            "..\\..\\Windows\\System32",
            "C:\\Windows",
            "D:\\Data",
            "/absolute/path",
            "FY2026-27/../../..",
            ".",
            "",
            None,
            123,
            {"fy": "hack"}
        ]
        for attack in attacks:
            self.assertFalse(app.is_safe_fy(attack), f"Attack '{attack}' should fail is_safe_fy")

        # Test /api/set-fy rejection
        for attack in attacks:
            res = self.client.post('/api/set-fy', json={"fy": attack})
            self.assertEqual(res.status_code, 400, f"set-fy did not return 400 for {attack}")

    def test_malformed_json_payloads(self):
        """Verify API endpoints handle malformed or non-dict payloads gracefully."""
        # Non-JSON content
        res = self.client.post('/api/manual-match', data="not json", content_type="text/plain")
        self.assertEqual(res.status_code, 400)

        # Empty body
        res = self.client.post('/api/manual-match', data="", content_type="application/json")
        self.assertEqual(res.status_code, 400)

        # Array root payload instead of object
        res = self.client.post('/api/set-fy', json=[1, 2, 3])
        self.assertEqual(res.status_code, 400)

        res = self.client.post('/api/manual-match', json=[1, 2, 3])
        self.assertEqual(res.status_code, 400)

        res = self.client.post('/api/unlink', json=[1, 2, 3])
        self.assertEqual(res.status_code, 400)

        res = self.client.post('/api/sticky-notes', json="just a string")
        self.assertEqual(res.status_code, 400)

    def test_all_fys_read_only_protection(self):
        """Verify __ALL__ view blocks write operations with HTTP 400."""
        with self.client.session_transaction() as sess:
            sess['fy'] = '__ALL__'

        res = self.client.post('/api/manual-match', json={"bankIds": ["1"], "glIds": ["1"]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Read-only", res.get_json().get("error", ""))

        res = self.client.post('/api/unlink', json={"bankIds": ["1"], "glIds": ["1"]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Read-only", res.get_json().get("error", ""))

        # Notes stay writable in the All-FY view, but only with an FY-prefixed id.
        res = self.client.post('/api/sticky-notes', json={"id": "1", "note": "test"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("financial-year prefix", res.get_json().get("error", ""))
        res = self.client.post('/api/sticky-notes', json={"id": "../evil:1", "note": "test"})
        self.assertEqual(res.status_code, 400)

    def test_all_fys_notes_route_to_owning_fy(self):
        """A note saved in the All-FY view lands in that FY's sticky_notes.json."""
        with self.client.session_transaction() as sess:
            sess['fy'] = '__ALL__'
        notes_file = os.path.join(self.sandbox_path, 'sticky_notes.json')
        tx = 'ZZ_ALLFY_TEST'
        try:
            res = self.client.post('/api/sticky-notes', json={"id": f"{self.sandbox_fy}:{tx}", "note": "from all-fy"})
            self.assertEqual(res.status_code, 200, res.get_json())
            self.assertEqual(res.get_json()["notes"][f"{self.sandbox_fy}:{tx}"]["text"], "from all-fy")
            with open(notes_file, encoding='utf-8') as f:
                self.assertEqual(json.load(f)[tx]["text"], "from all-fy")
            res = self.client.post('/api/sticky-notes/batch', json={"ids": [f"{self.sandbox_fy}:{tx}"], "action": "resolve"})
            self.assertEqual(res.status_code, 200, res.get_json())
            with open(notes_file, encoding='utf-8') as f:
                self.assertEqual(json.load(f)[tx]["status"], "resolved")
        finally:
            self.client.post('/api/sticky-notes', json={"id": f"{self.sandbox_fy}:{tx}", "note": ""})

    def test_corrupt_persistence_files_abort_with_500_without_wiping(self):
        """Verify corrupted JSON files cause 500 error instead of silently wiping data."""
        with self.client.session_transaction() as sess:
            sess['fy'] = self.sandbox_fy

        # Corrupt manual_links.json
        with open(self.manual_links_path, 'wb') as f:
            f.write(b"CORRUPT_JSON_DATA_HERE")
        if os.path.exists(self.manual_links_path + ".bak"):
            os.remove(self.manual_links_path + ".bak")

        # /api/manual-match should return 500
        res = self.client.post('/api/manual-match', json={"bankIds": ["101"], "glIds": ["1"]})
        self.assertEqual(res.status_code, 500)
        self.assertIn("unreadable", res.get_json().get("error", ""))

        # Verify corrupt file is intact (not overwritten or truncated)
        with open(self.manual_links_path, 'rb') as f:
            self.assertEqual(f.read(), b"CORRUPT_JSON_DATA_HERE")

        # Corrupt sticky_notes.json
        with open(self.sticky_notes_path, 'wb') as f:
            f.write(b"CORRUPT_NOTES_DATA")
        if os.path.exists(self.sticky_notes_path + ".bak"):
            os.remove(self.sticky_notes_path + ".bak")

        res_notes = self.client.post('/api/sticky-notes', json={"id": "101", "note": "test note"})
        self.assertEqual(res_notes.status_code, 500)
        with open(self.sticky_notes_path, 'rb') as f:
            self.assertEqual(f.read(), b"CORRUPT_NOTES_DATA")

    # ---------------------------------------------------------
    # 4. Double-Entry Unlinking Logic & Multi-ID Behavior
    # ---------------------------------------------------------
    def test_double_entry_unlinking_and_orphan_protection(self):
        """Verify partial unlinking, orphan protection, and internal offset unlinking."""
        with self.client.session_transaction() as sess:
            sess['fy'] = self.sandbox_fy

        df_b, df_g = app.get_cached_data(self.sandbox_fy)
        b_ids = df_b['bank_id'].tolist() # ['101', '102', '103', '104']
        g_ids = df_g['gl_id'].tolist()   # [g0, g1, g2, g3, g4, g5, g6]

        b1, b2, b3 = b_ids[0], b_ids[1], b_ids[2]
        g1, g2, g3, g4, g5 = g_ids[0], g_ids[1], g_ids[2], g_ids[3], g_ids[4]

        # Case A: Standard 1:1 match unlinking
        initial_matches = [
            {"type": "MANUAL", "bank_ids": [b1], "gl_ids": [g1]},
            {"type": "MANUAL", "bank_ids": [b2, b3], "gl_ids": [g2]}, # 2:1 match
            {"type": "MANUAL", "bank_ids": [], "gl_ids": [g3, g4, g5]}, # 3-way internal offset
        ]
        storage.save_json(self.manual_links_path, initial_matches, backup=False)

        # Unlink 1:1 match (b1, g1) -> drops match
        res = self.client.post('/api/unlink', json={"bankIds": [b1], "glIds": [g1]})
        self.assertEqual(res.status_code, 200)
        matches = storage.load_json(self.manual_links_path)
        self.assertEqual(len(matches), 2)

        # Case B: Partial unlinking from 2:1 match (remove b2 from [b2, b3] & [g2])
        # Remaining is b3 & g2 (1 Bank, 1 GL -> valid double entry)
        res = self.client.post('/api/unlink', json={"bankIds": [b2], "glIds": []})
        self.assertEqual(res.status_code, 200)
        matches = storage.load_json(self.manual_links_path)
        self.assertEqual(len(matches), 2)
        match_2_1 = [m for m in matches if g2 in m["gl_ids"]][0]
        self.assertEqual(match_2_1["bank_ids"], [b3])
        self.assertEqual(match_2_1["gl_ids"], [g2])

        # Case C: Orphan Protection (remove b3 from [b3] & [g2])
        # Remaining would be 0 Bank and 1 GL -> invalid double entry -> entire match must dissolve
        res = self.client.post('/api/unlink', json={"bankIds": [b3], "glIds": []})
        self.assertEqual(res.status_code, 200)
        matches = storage.load_json(self.manual_links_path)
        self.assertEqual(len(matches), 1, "Orphaned GL entry should have been dissolved")
        self.assertNotIn(g2, str(matches))

        # Case D: Internal Offset unlinking (remove g3 from [g3, g4, g5])
        # Remaining is [g4, g5] (2 GL entries, 0 Bank -> valid internal offset)
        res = self.client.post('/api/unlink', json={"bankIds": [], "glIds": [g3]})
        self.assertEqual(res.status_code, 200)
        matches = storage.load_json(self.manual_links_path)
        self.assertEqual(len(matches), 1)
        self.assertEqual(set(matches[0]["gl_ids"]), {g4, g5})

        # Case E: Breaking Internal Offset (remove g4 from [g4, g5])
        # Remaining is [g5] (1 GL entry, 0 Bank -> invalid single entry -> dissolved)
        res = self.client.post('/api/unlink', json={"bankIds": [], "glIds": [g4]})
        self.assertEqual(res.status_code, 200)
        matches = storage.load_json(self.manual_links_path)
        self.assertEqual(len(matches), 0, "Breaking internal offset below 2 entries must dissolve match")

    def test_unlink_nonexistent_match(self):
        """Verify unlinking non-existent IDs returns 404."""
        with self.client.session_transaction() as sess:
            sess['fy'] = self.sandbox_fy
        storage.save_json(self.manual_links_path, [{"bank_ids": ["B1"], "gl_ids": ["G1"]}], backup=False)

        res = self.client.post('/api/unlink', json={"bankIds": ["NON_EXISTENT"], "glIds": ["NON_EXISTENT"]})
        self.assertEqual(res.status_code, 404)

    def test_unlink_all_manual(self):
        """Verify allManual: true clears all manual links and saves backup."""
        with self.client.session_transaction() as sess:
            sess['fy'] = self.sandbox_fy
        initial = [{"bank_ids": ["B1"], "gl_ids": ["G1"]}, {"bank_ids": ["B2"], "gl_ids": ["G2"]}]
        storage.save_json(self.manual_links_path, initial, backup=False)

        res = self.client.post('/api/unlink', json={"allManual": True})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(storage.load_json(self.manual_links_path), [])
        # Verify backup exists
        self.assertTrue(os.path.exists(self.manual_links_path + ".bak"))
        with open(self.manual_links_path + ".bak", 'r', encoding='utf-8') as f:
            self.assertEqual(json.load(f), initial)

    # ---------------------------------------------------------
    # 5. Sticky Notes CRUD and Character Limits
    # ---------------------------------------------------------
    def test_sticky_notes_lifecycle(self):
        """Verify sticky notes creation, editing, deletion, and length validation."""
        with self.client.session_transaction() as sess:
            sess['fy'] = self.sandbox_fy

        # Missing transaction ID
        res = self.client.post('/api/sticky-notes', json={"id": "", "note": "some note"})
        self.assertEqual(res.status_code, 400)

        # Note exceeding max length (5000 chars)
        res = self.client.post('/api/sticky-notes', json={"id": "101", "note": "A" * 5001})
        self.assertEqual(res.status_code, 400)
        self.assertIn("too long", res.get_json().get("error", "").lower())

        # Create note
        res = self.client.post('/api/sticky-notes', json={"id": "101", "note": "Audited payment", "category": "Verified"})
        self.assertEqual(res.status_code, 200)
        notes = res.get_json().get("notes", {})
        self.assertEqual(notes.get("101")["text"], "Audited payment")
        self.assertEqual(notes.get("101")["category"], "Verified")
        self.assertIn("type", notes.get("101"))
        self.assertIn("status", notes.get("101"))

        # Update note
        res = self.client.post('/api/sticky-notes', json={"id": "101", "note": "Updated audit note", "category": "Remark"})
        self.assertEqual(res.status_code, 200)
        notes = storage.load_json(self.sticky_notes_path)
        self.assertEqual(notes.get("101")["text"], "Updated audit note")
        self.assertEqual(notes.get("101")["category"], "Remark")

        # Delete note (empty text)
        res = self.client.post('/api/sticky-notes', json={"id": "101", "note": ""})
        self.assertEqual(res.status_code, 200)
        notes = storage.load_json(self.sticky_notes_path)
        self.assertNotIn("101", notes)

    # ---------------------------------------------------------
    # 6. Caching, Gzip, and ETag
    # ---------------------------------------------------------
    def test_etag_and_304_short_circuit(self):
        """Verify ETag calculation and 304 Not Modified caching on /api/data."""
        with self.client.session_transaction() as sess:
            sess['fy'] = self.sandbox_fy

        # First request performs reconcile / loads cache
        res1 = self.client.get('/api/data')
        self.assertEqual(res1.status_code, 200)
        etag1 = res1.headers.get('ETag')
        self.assertIsNotNone(etag1, "ETag must be returned on cached /api/data")

        # Second request with If-None-Match header
        res2 = self.client.get('/api/data', headers={'If-None-Match': etag1})
        self.assertEqual(res2.status_code, 304)
        self.assertEqual(len(res2.get_data()), 0, "304 response must not have body payload")

        # Modify cached file (simulate quick save or match change)
        time.sleep(0.01) # ensure mtime changes
        cached_data = storage.load_json(self.data_cache_path)
        cached_data["summary"]["custom_field"] = 999
        storage.save_json(self.data_cache_path, cached_data, backup=False)

        # Third request with old ETag should now return 200 with new ETag
        res3 = self.client.get('/api/data', headers={'If-None-Match': etag1})
        self.assertEqual(res3.status_code, 200)
        etag3 = res3.headers.get('ETag')
        self.assertNotEqual(etag1, ettag := etag3)

    def test_session_cookie_persistence_across_restarts(self):
        """Verify session cookie generated by one client is valid across client instances/restarts."""
        # Client 1 sets FY
        c1 = app.app.test_client()
        res_set = c1.post('/api/set-fy', json={"fy": self.sandbox_fy})
        self.assertEqual(res_set.status_code, 200)

        # Extract session cookie
        cookie = c1.get_cookie('session')
        self.assertIsNotNone(cookie, "Session cookie must be set")

        # Client 2 (representing a new session/server restart reusing the persisted secret key)
        c2 = app.app.test_client()
        c2.set_cookie('session', cookie.value)

        # Check current-fy on client 2
        res_curr = c2.get('/api/current-fy')
        self.assertEqual(res_curr.status_code, 200)
        self.assertEqual(res_curr.get_json().get("fy"), self.sandbox_fy)

if __name__ == '__main__':
    unittest.main()
