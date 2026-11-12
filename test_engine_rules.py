"""Regression tests for the Nov-2026 engine audit: tag classifier, CMS loader
hygiene, double-transfer fail-safe, copied-narration detection, note-driven
corrections, note-type inference and the new matching tiers (2.2 / 3.3 / 3.4,
UTR-suffix and glued-prefix UTRs). Synthetic frames only - no project data."""
import os
import shutil
import tempfile
import unittest

import pandas as pd

import reconciler as R


def _row(side, rid, date, narr, amount, sign):
    dt = pd.to_datetime(date, dayfirst=True)
    refs = R.extract_refs(narr)
    rec = {'date': dt.strftime('%d-%m-%Y'), 'dt': dt, 'narration': narr, 'amount': float(amount),
           'type_sign': sign, 'txn_type': R.detect_type(narr), 'ref_id': refs[0] if refs else None,
           'all_refs': refs, 'cheque_no': R.extract_cheque(narr)}
    rec['bank_id' if side == 'bank' else 'gl_id'] = rid
    if side == 'gl':
        rec['original_refs'] = list(refs)
    return rec


def _reconcile(bank_rows, gl_rows):
    root = tempfile.mkdtemp(prefix='icici_rules_')
    try:
        os.makedirs(os.path.join(root, 'Bank Statement'))
        df_b, df_g = pd.DataFrame(bank_rows), pd.DataFrame(gl_rows)
        out = R.reconcile(df_bank=df_b, df_gl=df_g, data_root=root)
        return out, {g['gl_id']: m['status'] for m in out['matches'] for g in m['gls']}
    finally:
        shutil.rmtree(root, ignore_errors=True)


class TestTagClassifier(unittest.TestCase):
    def test_names_do_not_leak_into_tags(self):
        # Old substring scan tagged these EBA / VAT / MAT / IPS / SMO.
        for narr in ['UPI/611583849220/UPI/sheebapaniyan12/UNION BANK',
                     'RTGS/ICICR22026040415479989/FDRL0001088/ANEESH PATTUVATHI',
                     'UPI/610667137195/UPI/manayilrahmath@/INDIAN OVERSEAS',
                     'RTGS/NEFT To JIPSA K(A/c=67396250281), Ref No:151026153343']:
            tags = R.split_tags(R.detect_type(narr))
            for bogus in ('EBA', 'VAT', 'MAT', 'IPS', 'SMO', 'PAC', 'INF'):
                self.assertNotIn(bogus, tags, narr)

    def test_channels(self):
        self.assertEqual(R.split_tags(R.detect_type('MMT/IMPS/631414006119/FUND TRANSFER/SIBL0000160')), ['IMPS'])
        self.assertIn('RTGS', R.split_tags(R.detect_type('RTGS/NEFT To SKY GUIDE(A/c=1), Ref No:101126154912')))
        self.assertIn('TAX', R.split_tags(R.detect_type('GIB/002076985698/EPFO      /2611272027988')))
        self.assertIn('CHARGES', R.split_tags(R.detect_type('SGST202610077368617833')))
        self.assertIn('BBPS', R.split_tags(R.detect_type('BIL/BPAY/00000020NL4L/BBPS/KERALA WATER AUTH')))


class TestRefExtraction(unittest.TestCase):
    def test_glued_prefix_exposes_digit_tail(self):
        self.assertIn('8630755138261', R.extract_refs('TO UPI8630755138261/KAMALAKSHI P K'))

    def test_primary_ref_ignores_account_numbers(self):
        self.assertEqual(R.primary_txn_ref('RTGS/NEFT To NIDHA( A/c=110101043150), Ref No:030626120001'), '030626120001')
        self.assertEqual(R.primary_txn_ref('UPI/620482592955/UPI/sajithakrishnad/CANARA BANK/AXI7'), '620482592955')


class TestTransferAlerts(unittest.TestCase):
    def _cms(self, ref, serial, status, utr_raw, day, cust=None):
        return {'cms_ref': ref, 'serial': serial, 'utr': None, 'utr_raw': utr_raw, 'cust_ref': cust,
                'benef_name': 'SKY GUIDE', 'benef_acc': '13060200044088', 'amount': 142500.0,
                'dt': pd.Timestamp(2026, 11, day), 'status': status, 'mode': 'IMPS', 'ifsc': 'FDRL0001306'}

    def test_resent_payment_flagged_never_matched(self):
        cms = [self._cms('CMS5975798295', '101126154912', 'AWAITING MESSAGING', 'Final Status Awaited', 10),
               self._cms('CMS5976741742', 'CMS5976741742_1', 'PAID', 'IN22631526113893-Credited', 11, 'SIRAS-RETRY-NOV10')]
        gl = pd.DataFrame([_row('gl', 'g1', '10-11-2026', 'RTGS/NEFT To SKY GUIDE(A/c=13060200044088), Ref No:101126154912', 142500, 'CREDIT')])
        bank = pd.DataFrame([_row('bank', 'b1', '11-11-2026', 'CMS/002047766050/SIRAS-RETRY-NOV10', 142500, 'DEBIT')])
        alerts = R.apply_transfer_alerts(bank, gl, R.detect_transfer_alerts(cms))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['level'], 'RETRY_PENDING')
        self.assertIsInstance(gl.at[0, 'transfer_alert'], dict)
        self.assertIsInstance(bank.at[0, 'transfer_alert'], dict)
        _, status = _reconcile(bank.to_dict('records'), gl.to_dict('records'))
        self.assertNotIn('g1', status, 'a re-sent payment must never be auto-matched')

    def test_repeat_payments_with_own_vouchers_not_flagged(self):
        a = self._cms('CMS1', '000000000001', 'PAID', 'IN1234567890123', 10)
        b = self._cms('CMS2', '000000000002', 'PAID', 'IN1234567890124', 13)
        gl = pd.DataFrame([_row('gl', 'g1', '10-11-2026', 'RTGS/NEFT To X(A/c=1), Ref No:000000000001', 142500, 'CREDIT'),
                           _row('gl', 'g2', '13-11-2026', 'RTGS/NEFT To X(A/c=1), Ref No:000000000002', 142500, 'CREDIT')])
        self.assertEqual(R.apply_transfer_alerts(pd.DataFrame(), gl, R.detect_transfer_alerts([a, b])), [])


class TestCopiedNarration(unittest.TestCase):
    def test_double_post_flagged_split_not(self):
        gl = pd.DataFrame([
            _row('gl', 'a', '01-10-2026', 'UPI/627454062724/UPI/XXUCUC@OKICICI/ICICI BANK', 5000, 'DEBIT'),
            _row('gl', 'b', '05-10-2026', 'UPI/627454062724/UPI/XXUCUC@OKICICI/ICICI BANK', 5000, 'DEBIT'),
            _row('gl', 'c', '07-10-2026', 'UPI/111111111111/LOAN', 8000, 'DEBIT'),
            _row('gl', 'd', '07-10-2026', 'UPI/111111111111/INTEREST', 2000, 'DEBIT'),
        ])
        R.flag_copied_narrations(gl)
        self.assertTrue(gl.at[1, 'copied_narration']['same_amount'])
        self.assertIsNone(gl.at[0, 'copied_narration'])
        self.assertIsNone(gl.at[3, 'copied_narration'], 'same-day split must not be flagged')


class TestNoteCorrections(unittest.TestCase):
    def test_correct_equals_adds_utr(self):
        gl = pd.DataFrame([_row('gl', 'g1', '27-01-2026', 'UPI/999999999999/WRONG PERSON', 500, 'DEBIT')])
        notes = {'g1': {'text': 'Wrong narration. Correct = UPI/955448034498/Payment from Ph/x@yb'}}
        self.assertEqual(R.apply_note_corrections(gl, 'gl_id', notes), 1)
        self.assertIn('955448034498', gl.at[0, 'all_refs'])

    def test_incorrect_word_is_not_a_correction(self):
        gl = pd.DataFrame([_row('gl', 'g1', '27-01-2026', 'UPI/999999999999/X', 500, 'DEBIT')])
        R.apply_note_corrections(gl, 'gl_id', {'g1': {'text': 'Current narration is incorrect. Sabitha already done'}})
        self.assertIsNone(gl.at[0, 'corrected_narration'])


class TestNewTiers(unittest.TestCase):
    def test_late_utr_unique_any_gap(self):
        _, st = _reconcile([_row('bank', 'b1', '23-07-2026', 'UPI/620482592955/UPI/sajithakrishnad/CANARA BANK/AXI7', 7000, 'CREDIT')],
                           [_row('gl', 'g1', '23-09-2026', 'UPI/620482592955/UPI/SAJITHAKRISHNAD/CANARA BANK/AXI7', 7000, 'DEBIT')])
        self.assertEqual(st.get('g1'), 'TIER2.2_LATE_UTR_MATCH')

    def test_late_utr_refuses_duplicated_utr(self):
        _, st = _reconcile([_row('bank', 'b1', '23-07-2026', 'UPI/620482592955/UPI/x/CANARA BANK', 7000, 'CREDIT')],
                           [_row('gl', 'g1', '23-09-2026', 'UPI/620482592955/UPI/X/CANARA BANK', 7000, 'DEBIT'),
                            _row('gl', 'g2', '25-09-2026', 'UPI/620482592955/UPI/X/CANARA BANK', 7000, 'DEBIT')])
        self.assertNotIn('TIER2.2_LATE_UTR_MATCH', st.values())

    def test_rtgs_ifsc_and_statutory_and_suffix(self):
        bank = [_row('bank', 'b1', '05-11-2026', 'RTGS-IBKLR92026110500053997-VADAKKUMBAD SERVICE CO-OPERATIVE B', 3000000, 'CREDIT'),
                _row('bank', 'b2', '15-10-2026', 'GIB/002075795776/GST       /26103200047516', 3846, 'DEBIT'),
                _row('bank', 'b3', '15-10-2026', 'UPI/628827029672/UPI/XX9947@oksbi/State Bank Of I', 24, 'CREDIT'),
                _row('bank', 'b4', '03-11-2026', 'UPI/630755138261/UPI/XXpk82@okaxis/CANARA BANK', 2000, 'CREDIT')]
        gl = [_row('gl', 'g1', '05-11-2026', 'BY RTGS-TRANSFER - IDBI BANK', 3000000, 'DEBIT'),
              _row('gl', 'g2', '31-10-2026', 'GST PAID TO GOVT-NEETHI MEDICALS', 3846, 'CREDIT'),
              _row('gl', 'g3', '15-10-2026', 'UPI  29672', 24, 'DEBIT'),
              _row('gl', 'g4', '03-11-2026', 'TO UPI8630755138261/KAMALAKSHI P K', 2000, 'DEBIT')]
        _, st = _reconcile(bank, gl)
        self.assertEqual(st.get('g1'), 'TIER3.3_RTGS_IFSC_MATCH')
        self.assertEqual(st.get('g2'), 'TIER3.4_STATUTORY_MATCH')
        self.assertEqual(st.get('g3'), 'TIER3_EXACT_DATE')
        self.assertEqual(st.get('g4'), 'TIER2.1_FUZZY_UTR')

    def test_same_utr_different_amount_is_suggested_not_matched(self):
        out, st = _reconcile([_row('bank', 'b1', '08-10-2026', 'UPI/664781566698/UPI/XXjil9@okaxis/CANARA BANK', 76760, 'CREDIT')],
                             [_row('gl', 'g1', '08-10-2026', 'TO :  UPI/664781566698/UPI/XXJIL9@OKAXIS/CANARA BANK', 76600, 'DEBIT')])
        self.assertNotIn('g1', st)
        sug = out['missing_bank'][0]['suggestions']
        self.assertTrue(sug and sug[0]['amount_delta'] == 160.0)


class TestNoteTypeInference(unittest.TestCase):
    def test_inference_is_lossless_and_one_time(self):
        import app
        n = app.normalize_note_dict({'text': 'Wrong narration. Correct = UPI/1/x', 'type': 'NOTE', 'tags': ['a']})
        self.assertEqual(n['type'], 'WRONG_NARRATION')
        self.assertTrue(n['type_inferred'])
        self.assertEqual(n['text'], 'Wrong narration. Correct = UPI/1/x')
        self.assertEqual(n['tags'], ['a'])
        # A note already processed (type_inferred present) keeps the user's choice
        kept = app.normalize_note_dict({'text': 'Wrong narration', 'type': 'NOTE', 'type_inferred': False})
        self.assertEqual(kept['type'], 'NOTE')


if __name__ == '__main__':
    unittest.main()
