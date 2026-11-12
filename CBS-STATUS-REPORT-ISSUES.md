# Outward RTGS/NEFT/IMPS Status Report: Accuracy Issues

**To:** CBS software provider
**Report:** `RTGS_OUTWARD_STATUS_REP` (Oracle Reports export, `rtgs_outward_status_rep.xml`)
**Period checked:** 01-Apr-2026 to 11-Nov-2026 (1,273 outward payments)
**Checked against:** ICICI Bank CMS payment reports (source of truth for final status), bank statement and GL vouchers
**Date:** 12-Nov-2026

## Summary

1,253 of 1,273 rows (98.4%) are correct. The problems are:

1. **18 payments the bank paid still show `Confirmed/Send to Process`** (`OUT_FLAG = 2`), some since April. Total ₹4,99,300. The status was never updated after the bank's final response.
2. **15 of those 18 are IMPS to Kerala State Co-op Bank (`KSBK…`) IFSCs:** 15 of 21 KSBK IMPS payments (71%) are stuck. For other banks the stuck rate is about 0.2% (1 of 428 to SBI, 1 of 91 to Canara). This points to the final-status response for KSBK IMPS not being processed.
3. **The RTGS UTR in `CRN_NUM` is truncated** from 22 to 20 characters on all 141 RTGS rows. For example the report shows `ICICR220261009192735`, but the real UTR is `ICICR22026100919273522`. This UTR does not match the bank statement or CMS.
4. Some fields are missing (see section 3).

Failed payments are reported correctly. Every payment the bank cancelled shows `Failed/Reversed` (`OUT_FLAG = R`), and each has its reversal voucher posted.

## 1. How CMS final statuses appear in the CBS report

| Bank / CMS final status | Example CMS text | Should show in CBS | Actually shows in CBS | Result |
|---|---|---|---|---|
| PAID (IMPS) | `618214613777 - Transaction Success` | Success | **Success**, except 17 rows left at **Confirmed/Send to Process** (15 to KSBK IFSCs) | **Broken** |
| PAID (NEFT, credited later) | `IN22617029892680-Credited on 2026-06-19 17.09.00` | Success | **Confirmed/Send to Process** (1 row; NEFT credited 2 days after sending) | **Broken** |
| PAID (RTGS) | `ICICR22026091418730296` | Success | Success | OK, but UTR truncated (issue 3) |
| CANCELLED: `Invalid account` / `Incorrect Account Number` | `621815509031 - Invalid account` | Failed/Reversed | Failed/Reversed | OK |
| CANCELLED: `Transaction declined as initiated with invalid bene details` | | Failed/Reversed | Failed/Reversed | OK |
| CANCELLED: `Auto cancelled by system` | | Failed/Reversed | Failed/Reversed | OK |
| CANCELLED: RTGS return (`ACCOUNT DOES NOT EXIST`, `CREDIT TO NRI ACCOUNT`) | `ICICR22026100919273522-//…` | Failed/Reversed | Failed/Reversed | OK |
| AWAITING MESSAGING | `Final Status Awaited` | Confirmed/Send to Process | Confirmed/Send to Process | OK (genuinely pending) |
| Rejected before reaching the bank (e.g. invalid IFSC) | none (never sent) | Failed/Reversed | Failed/Reversed, but **`CRN_NUM` is blank** | OK, field missing |

## 2. Requested fixes

1. **Re-poll stale statuses.** Rows at `Confirmed/Send to Process` should be re-queried with the bank until a final status arrives, and not left pending after T+1 (IMPS) or T+2 (NEFT). Please also run a one-time correction of the 18 rows in the appendix.
2. **Check final-status handling for KSBK beneficiaries.** The response or callback for IMPS to `KSBK0001653`, `KSBK0001776` and `KSBK0001777` is not being applied in most cases. It may use a response code or format the CBS does not map.
3. **Handle late NEFT credit confirmations.** NEFT credited on a later day (`Credited on <date>`) should still update the row to Success.
4. **Store the full RTGS UTR** (22 characters) in `CRN_NUM` or a separate UTR field. The field width looks like it is 20.

## 3. Requested additions to the report

| Field | Why |
|---|---|
| `UTR` / `RRN`: IMPS RRN, NEFT UTR, full RTGS UTR | Lets each row match the bank statement directly |
| `BANK_STATUS_TEXT`: the bank's raw final status or reason (e.g. `Invalid account`) | Tells us why a payment failed |
| `STATUS_UPDATED_ON`: timestamp of the last status change | Shows how long a row has been waiting |
| A value in `CRN_NUM` (or a reason code) for rows rejected before sending | Today these rows have a blank CRN |
| `REVERSAL_VOUCHER_NO` for Failed/Reversed rows | Links the failure to its GL reversal |

## Appendix: rows to correct

The 18 rows below were paid by the bank but still show `Confirmed/Send to Process`. Beneficiary names are left out. Use the serial number or CMS ref to look each row up.

| Payment date | Serial no | CMS ref | Mode | Beneficiary IFSC | Amount (₹) | CBS status | Bank (CMS) final status |
|---|---|---|---|---|---:|---|---|
| 04-Apr-26 | 040426122159 | CMS5620306693 | IMPS | KSBK0001653 | 25,000.00 | Confirmed/Send to Process | PAID: 609412263030 Transaction Success |
| 04-Apr-26 | 040426135509 | CMS5620550571 | IMPS | KSBK0001776 | 7,000.00 | Confirmed/Send to Process | PAID: 609414489408 Transaction Success |
| 13-Apr-26 | 130426173419 | CMS5633156152 | IMPS | KSBK0001653 | 10,000.00 | Confirmed/Send to Process | PAID: 610317109196 Transaction Success |
| 18-Apr-26 | 180426102724 | CMS5640834572 | IMPS | KSBK0001776 | 40,000.00 | Confirmed/Send to Process | PAID: 610810235830 Transaction Success |
| 02-May-26 | 020526101548 | CMS5659428369 | IMPS | KSBK0001653 | 16,500.00 | Confirmed/Send to Process | PAID: 612210058710 Transaction Success |
| 07-May-26 | 070526152538 | CMS5667823362 | IMPS | KSBK0001777 | 10,000.00 | Confirmed/Send to Process | PAID: 612716640753 Transaction Success |
| 18-May-26 | 180526125805 | CMS5681465735 | IMPS | KSBK0001776 | 1,15,000.00 | Confirmed/Send to Process | PAID: 613813187413 Transaction Success |
| 01-Jun-26 | 010626101355 | CMS5698332702 | IMPS | KSBK0001653 | 17,500.00 | Confirmed/Send to Process | PAID: 615210486563 Transaction Success |
| 16-Jun-26 | 160626111726 | CMS5722240996 | IMPS | KSBK0001777 | 15,000.00 | Confirmed/Send to Process | PAID: 616711254100 Transaction Success |
| 17-Jun-26 | 170626155654 | CMS5724210006 | NEFT | UBIN0536172 | 18,000.00 | Confirmed/Send to Process | PAID: IN22617029892680 Credited on 19-Jun-26 |
| 01-Jul-26 | 010726121516 | CMS5743478017 | IMPS | KSBK0001776 | 5,000.00 | Confirmed/Send to Process | PAID: 618214613777 Transaction Success |
| 01-Jul-26 | 010726121613 | CMS5743478040 | IMPS | KSBK0001776 | 7,000.00 | Confirmed/Send to Process | PAID: 618214613702 Transaction Success |
| 14-Jul-26 | 140726153146 | CMS5769325094 | IMPS | KSBK0001776 | 20,800.00 | Confirmed/Send to Process | PAID: 619516139448 Transaction Success |
| 24-Jul-26 | 240726112013 | CMS5785136400 | IMPS | KSBK0001776 | 9,500.00 | Confirmed/Send to Process | PAID: 620512461875 Transaction Success |
| 01-Aug-26 | 010826122314 | CMS5798284652 | IMPS | KSBK0001653 | 23,000.00 | Confirmed/Send to Process | PAID: 621312638482 Transaction Success |
| 11-Aug-26 | 110826160009 | CMS5821245867 | IMPS | CNRB0000861 | 1,00,000.00 | Confirmed/Send to Process | PAID: 622510647575 Transaction Success |
| 11-Aug-26 | 110826162615 | CMS5821296625 | IMPS | SBIN0005048 | 30,000.00 | Confirmed/Send to Process | PAID: 622510648502 Transaction Success |
| 14-Aug-26 | 140826112754 | CMS5825741121 | IMPS | KSBK0001776 | 30,000.00 | Confirmed/Send to Process | PAID: 622612699607 Transaction Success |
| **Total** | | | | | **4,99,300.00** | | |

**Correctly pending, no action needed:** serial 101126154912 (10-Nov-26, ₹1,42,500, bank shows `Final Status Awaited`) and serial 111126161713 (11-Nov-26, ₹10,000, no bank status yet).

**Correctly failed but CRN blank (rejected before sending):** serials 050526105701, 140926134631 and 140926143512.
