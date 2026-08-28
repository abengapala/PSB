"""
PSB NEW ENDO — Agent Submission + Admin DataGrid Duplicate Checker
====================================================================
(original app — see full original docstring content further below in
comments; behavior is UNCHANGED)

====================================================================
CHANGELOG — READ THIS FIRST (next AI / next session, start here)
====================================================================
Date added: 2026-08-21
Added by : Claude, per Urban's instructions.

WHAT CHANGED
------------
Added a second, fully independent process called "AUTO STAT" alongside
the existing "NEW ENDO" process. Nothing about the original Endo logic,
its Google Sheet tabs ("Submissions", "DataGrid"), its functions
(agent_form_page, admin_dashboard, insert_submission, load_submissions,
replace_datagrid_accounts, load_datagrid_set, mark_exported,
build_export_workbook, normalize_account, init_db's original two
_get_or_create_ws calls) was modified. Those functions are untouched
copy-paste from the original file.

WHAT AUTO STAT IS (business context, from Urban)
-------------------------------------------------
Agents used to: (1) fill out an external MS/Google Form per account
call ("AUTOSTAT FORM" export: Account Number, Submitted on,
Respondents, Status Code, Remarks, PTP Date, PTP Amount, Remark Date,
Claim Paid Date, Claim Paid Amount, CMS Username, Status, Person),
(2) manually paste that into columns K–V of an "AUTOSTAT" tab in the
PSB Campaign Portfolio workbook, (3) manually rebuild a "clean" A–G
view (New Account Number, Status Code, Remarks, Remarks Date, PTP
Date, PTP Amount, Collector), (4) manually fix duplicate account
numbers by nudging the REMARKS DATE of the duplicate by +1 minute so
two rows never share an identical timestamp, and (5) manually pull out
any row that has a Claim Paid Date/Amount into a separate sheet,
because those rows must NOT be uploaded as normal remarks (the system
already knows about the claim payment through another channel — a
duplicate remarks upload would be wrong).

This update replaces steps (1)-(5) with a Streamlit form + automated
export inside THIS app, using the SAME Google Sheet database as Endo
(new tabs only, nothing shared/overwritten).

REQUIRED OUTPUT FORMAT (confirmed by Urban via screenshot — do not
change without him explicitly asking):
  - REMARKS DATE : MM/DD/YYYY HH:MM:SS   (e.g. 07/15/2026 08:52:00)
  - PTP DATE     : MM/DD/YYYY only, no time (e.g. 07/18/2026)
  - PTP AMOUNT   : plain number, 2 decimals, no currency sign/commas
  - COLLECTOR    : CMS username, UPPERCASE

NEW GOOGLE SHEET TAB
---------------------
"AutoStat_Submissions" — created automatically on first run, exactly
like the existing tabs. Columns: id, account_number, status_code,
remarks, ptp_date, ptp_amount, claim_paid_date, claim_paid_amount,
collector, submitted_at, exported, exported_at.

NEW UI FLOW
------------
- Agent side: landing page now asks the agent to pick "New Endo" or
  "Auto Stat" first (process_picker_page). Picking Endo goes straight
  to the ORIGINAL, unmodified agent_form_page(). Picking Auto Stat
  goes to the new autostat_form_page().
- Admin side: after password login, admin now picks "Endo" or "Auto
  Stat" first (admin_router / admin_picker_page). Picking Endo calls
  the ORIGINAL, unmodified admin_dashboard(). Picking Auto Stat calls
  the new autostat_admin_dashboard().

NEW LOGIC — DEDUPE + SEGREGATE (build_autostat_export_workbook)
------------------------------------------------------------------
Applied ONLY at export time, on a COPY of the data (never mutates the
stored submissions):
  1. Group by account_number. For the 2nd, 3rd, ... occurrence of the
     same account number, add +1 minute (x2, x3, ...) to that row's
     REMARKS DATE (submitted_at) so timestamps never collide, matching
     Urban's manual "8:30 -> 8:31" fix.
  2. Split rows into two groups:
       - No Claim Paid Date AND no Claim Paid Amount -> "CLEAN" sheet
         (columns: NEW ACCOUNT NUMBER, STATUS CODE, REMARKS, REMARKS
         DATE, PTP DATE, PTP AMOUNT, COLLECTOR) — this is what gets
         uploaded as remarks.
       - Has a Claim Paid Date OR Claim Paid Amount -> "CLAIM PAID"
         sheet (same columns + CLAIM PAID DATE, CLAIM PAID AMOUNT) —
         excluded from the remarks upload, kept separate.
  Both sheets live in ONE downloaded .xlsx workbook (two tabs), per
  Urban's "they go to separate sheet" instruction.

ASSUMPTIONS MADE (flag to Urban, adjust if wrong)
----------------------------------------------------
- STATUS CODE on the Auto Stat form is a free-text field (like the
  original AUTOSTAT FORM had many different values e.g. "CALL - PTP
  FULL UPDATE", "CALL - PTP REPO"). If Urban wants a fixed dropdown of
  allowed status codes, that's a follow-up change.
  - "Respondents" and "Status" (col L/M from the original AUTOSTAT
  FORM) were NOT carried over as form fields — they didn't appear in
  Urban's required output format and looked mostly unused/blank in the
  sample data. Collector/CMS Username is captured instead, matching
  the "COLLECTOR" column in the required output.
- Duplicate detection is scoped to whatever is included in a given
  export run (i.e. not-yet-exported rows, after any admin filters),
  not the entire all-time submission history. Exported rows are marked
  exported=1 after download, same pattern as Endo, so they won't be
  re-included/re-bumped in a future export.
====================================================================
"""

import math
import re
import uuid
from datetime import date, datetime
from io import BytesIO

import gspread
import numpy as np
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

# ----------------------------------------------------------------------
# CONFIG — change these to fit your team
# ----------------------------------------------------------------------
PLACEMENTS = ["FRONTEND", "MIDRANGE", "HARDCORE"]

STATUS_CODES = [
    "CALL - POS_UNATTENDED",
    "CALL - POS_KOR",
    "CALL - POS_DROPPED",
    "CALL - POS_BUSY",
    "CALL - POS_LEAVE MSG TO 3RD PARTY",
    "CALL - UNDERNEGO",
    "CALL - CLAIMING PAID",
    "CALL - INSURANCE CLAIM",
    "CALL - UNIT_IMPOUNDED",
    "CALL - UNIT UNDER HPG",
    "CALL - UNIT_ASSUMED",
    "CALL - UNIT DAMAGE OR WRECK",
    "CALL - UNIT_CARNAPPED",
    "CALL - NO INTENTION TO PAY",
    "CALL - PTP REPO",
    "CALL - PTP PAYOFF",
    "CALL - PTP FULL UPDATE",
    "CALL - PTP PUSH BACK",
    "CALL - PTP PARTIAL",
    "CALL - FOLLOW UP KOR",
    "CALL - FOLLOW UP UNCONTACTABLE",
    "CALL - FOLLOW UP LMTRC",
    "CALL - FOLLOW UP COMPLIANT",
    "CALL - POS_CBR",
    "CALL - NEG_UNATTENDED",
    "CALL - NEG_KOR",
    "CALL - NEG_DROPPED",
    "CALL - NEG_WRONG NUMBER",
    "CALL - NEG_LEAVE MSG TO 3RD PARTY",
    "CALL - NEG_EMPLOYER NLC",
    "CALL - NEG_BUSY",
    "CALL - NEG_NOT IN SERVICE",
    "CALL - DECEASED",
    "CALL - NEG_CBR",
    "CALL - KEPT_REPO CLIENT",
    "CALL - KEPT_REPO 3RD PARTY",
    "CALL - KEPT PAYOFF",
    "CALL - KEPT_FULL UPDATE",
    "CALL - KEPT_PUSH BACK",
    "CALL - KEPT_PARTIAL",
    "SMS - NEG_SENT MESSAGE",
    "SMS - DECEASED",
    "SMS - WRONG NUMBER",
    "SMS - POS_SENT MESSAGE",
    "SMS - RESPONSIVE",
    "SMS - GOT NEW CONTACT NUM",
    "SMS - PTP REPO",
    "SMS - PTP PAYOFF",
    "SMS - PTP FULL UPDATE",
    "SMS - PTP PUSH BACK",
    "SMS - PTP_PARTIAL",
    "SMS - CLAIMING PAID",
    "SMS - INSURANCE CLAIM",
    "SMS - UNIT IMPOUNDED",
    "SMS - UNDER HPG",
    "SMS - UNIT ASSUMED",
    "SMS - UNIT DAMAGE OR WRECK",
    "SMS - UNIT CARNAPPED",
    "SMS - NO INTENTION TO PAY",
    "SMS - FOLLOW UP MESSAGE",
    "SMS - FOLLOW UP COMPLIANT",
    "SMS - KEPT_REPO CLIENT",
    "SMS - KEPT_REPO 3RD PARTY",
    "SMS - KEPT PAYOFF",
    "SMS - KEPT_FULL UPDATE",
    "SMS - KEPT_PUSH BACK",
    "SMS - KEPT_PARTIAL",
    "EMAIL - NEG_SENT MESSAGE",
    "EMAIL - DECEASED",
    "EMAIL - POS_SENT MESSAGE",
    "EMAIL - RESPONSIVE",
    "EMAIL - GOT NEW CONTACT",
    "EMAIL - PTP REPO",
    "EMAIL - PTP PAYOFF",
    "EMAIL - PTP FULL UPDATE",
    "EMAIL - PTP PUSH BACK",
    "EMAIL - PTP_PARTIAL",
    "EMAIL - CLAIMING PAID",
    "EMAIL - INSURANCE CLAIM",
    "EMAIL - UNIT CARNAPPED",
    "EMAIL - UNIT UNDER HPG",
    "EMAIL - NO INTENTION TO PAY",
    "EMAIL - UNIT_IMPOUNDED",
    "EMAIL - UNIT ASSUMED",
    "EMAIL - UNIT DAMAGE OR WRECK",
    "EMAIL - FOLLOW UP MESSAGE",
    "EMAIL - FOLLOW UP COMPLIANT",
    "EMAIL - CEASE COLLECTION",
    "EMAIL - KEPT_REPO CLIENT",
    "EMAIL - KEPT_REPO 3RD PARTY",
    "EMAIL - KEPT PAYOFF",
    "EMAIL - KEPT_FULL UPDATE",
    "EMAIL - KEPT_PUSH BACK",
    "EMAIL - KEPT_PARTIAL",
    "SKIP - NEGATIVE",
    "SKIP - SMEDIA ACCOUNT",
    "SKIP - NEW ADDRESS",
    "SKIP - CONTACT NUMBER",
    "SKIP - POSSIBLE LEADS",
    "SKIP - UNIT CARNAPPED",
    "SKIP - UNIT UNDER HPG",
    "SKIP - UNIT IMPOUNDED",
    "SKIP - UNIT ASSUMED",
    "SKIP - UNIT DAMAGE OR WRECK",
    "SKIP - KEPT_REPO CLIENT",
    "SKIP - KEPT_REPO 3RD PARTY",
    "SKIP - KEPT PAYOFF",
    "SKIP - KEPT_FULL UPDATE",
    "SKIP - KEPT_PUSH BACK",
    "SKIP - KEPT_PARTIAL",
    "SMEDIA - NEG_SENT A MESSAGE",
    "SMEDIA - POS_SENT A MESSAGE",
    "SMEDIA - RESPONSIVE",
    "SMEDIA - PTP REPO",
    "SMEDIA - PTP PAYOFF",
    "SMEDIA - PTP FULL UPDATE",
    "SMEDIA - PTP PUSH BACK",
    "SMEDIA - PTP PARTIAL",
    "SMEDIA - FOLLOW UP MESSAGE",
    "SMEDIA - FOLLOW UP COMPLIANT",
    "SMEDIA - CLAIMING PAID",
    "SMEDIA - INSURANCE CLAIM",
    "SMEDIA - UNIT CARNAPPED",
    "SMEDIA - UNIT UNDER HPG",
    "SMEDIA - UNIT IMPOUNDED",
    "SMEDIA - UNIT ASSUMED",
    "SMEDIA - UNIT DAMAGE OR WRECK",
    "SMEDIA - NO INTENTION TO PAY",
    "SMEDIA - KEPT_REPO CLIENT",
    "SMEDIA - KEPT_REPO 3RD PARTY",
    "SMEDIA - KEPT PAYOFF",
    "SMEDIA - KEPT_FULL UPDATE",
    "SMEDIA - KEPT_PUSH BACK",
    "SMEDIA - KEPT_PARTIAL",
    "FIELD - UNLOCATED",
    "FIELD - CLIENT_UNKNOWN",
    "FIELD - CLIENT_OUT OF AREA",
    "FIELD - NOT_ALLOWED TO ENTER",
    "FIELD - DECEASED",
    "FIELD - LOT_ONLY",
    "FIELD - LEAVE_MESSAGE TO 3RD PARTY",
    "FIELD - HOUSED_CLOSED UNVERIFIED",
    "FIELD - HOUSED CLOSED VERIFIED",
    "FIELD - MOVED_OUT",
    "FIELD - RESULT",
    "FIELD - PTP REPO",
    "FIELD - PTP_FULL UPDATE",
    "FIELD - PTP_PAYOFF",
    "FIELD - PTP_PUSHBACK",
    "FIELD - PTP_PARTIAL",
    "FIELD - FOLLOW UP COMPLIANT",
    "FIELD - CLAIMING PAID",
    "FIELD - INSURANCE CLAIM",
    "FIELD - UNIT CARNAPPED",
    "FIELD - UNIT UNDER HPG",
    "FIELD - UNIT IMPOUNDED",
    "FIELD - UNIT ASSUMED",
    "FIELD - UNIT DAMAGE OR WRECK",
    "FIELD - NO INTENTION TO PAY",
    "FIELD - KEPT_REPO CLIENT",
    "FIELD - KEPT_REPO 3RD PARTY",
    "FIELD - KEPT PAYOFF",
    "FIELD - KEPT_FULL UPDATE",
    "FIELD - KEPT_PUSH BACK",
    "FIELD - KEPT_PARTIAL",
    "CARAVAN - UNLOCATED",
    "CARAVAN - CLIENT UNKNOWN",
    "CARAVAN - CLIENT OUT OF AREA",
    "CARAVAN - NOT ALLOWED TO ENTER",
    "CARAVAN - DECEASED",
    "CARAVAN - LOT ONLY",
    "CARAVAN - LEAVE MESSAGE TO 3RD PARTY",
    "CARAVAN - HOUSED CLOSED UNVERIFIED",
    "CARAVAN - HOUSED CLOSED VERIFIED",
    "CARAVAN - MOVED OUT",
    "CARAVAN - RESULT",
    "CARAVAN - PTP REPO",
    "CARAVAN - PTP FULL UPDATE",
    "CARAVAN - PTP PAYOFF",
    "CARAVAN - PTP PUSHBACK",
    "CARAVAN - PTP PARTIAL",
    "CARAVAN - FOLLOW UP COMPLIANT",
    "CARAVAN - CLAIMING PAID",
    "CARAVAN - INSURANCE CLAIM",
    "CARAVAN - UNIT CARNAPPED",
    "CARAVAN - UNIT UNDER HPG",
    "CARAVAN - UNIT IMPOUNDED",
    "CARAVAN - UNIT ASSUMED",
    "CARAVAN - UNIT DAMAGE OR WRECK",
    "CARAVAN - NO INTENTION TO PAY",
    "CARAVAN - KEPT_REPO CLIENT",
    "CARAVAN - KEPT_REPO 3RD PARTY",
    "CARAVAN - KEPT PAYOFF",
    "CARAVAN - KEPT_FULL UPDATE",
    "CARAVAN - KEPT_PUSH BACK",
    "CARAVAN - KEPT_PARTIAL",
    "REPO AI - PTP REPO",
    "REPO AI - PTP FULL UPDATE",
    "REPO AI - PTP PAY OFF",
    "REPO AI - PTP PUSHBACK",
    "REPO AI - PTP PARTIAL",
    "REPO AI - KEPT_REPO CLIENT",
    "REPO AI - KEPT_REPO 3RD PARTY",
    "REPO AI - KEPT PAYOFF",
    "REPO AI - KEPT_FULL UPDATE",
    "REPO AI - KEPT_PUSH BACK",
    "REPO AI - KEPT_PARTIAL",
    "VIBER - DELIVERED",
    "VIBER - READ",
    "VIBER - PENDING",
    "VIBER - BOUNCED",
    "VIBER - POS_SENT A MESSAGE",
    "VIBER - NEG_SENT A MESSAGE",
    "VIBER - RESPONSIVE",
    "VIBER - PTP REPO",
    "VIBER - PTP PAYOFF",
    "VIBER - PTP FULL UPDATE",
    "VIBER - PTP PUSH BACK",
    "VIBER - PARTIAL",
    "VIBER - FOLLOW UP MESSAGE",
    "VIBER - FOLLOW UP COMPLIANT",
    "VIBER - CLAIMING PAID",
    "VIBER - INSURANCE CLAIM",
    "VIBER - UNIT CARNAPPED",
    "VIBER - UNIT UNDER HPG",
    "VIBER - UNIT IMPOUNDED",
    "VIBER - UNIT ASSUMED",
    "VIBER - UNIT DAMAGE OR WRECK",
    "VIBER - NO INTENTION TO PAY",
    "VIBER - KEPT_REPO CLIENT",
    "VIBER - KEPT_REPO 3RD PARTY",
    "VIBER - KEPT PAYOFF",
    "VIBER - KEPT_FULL UPDATE",
    "VIBER - KEPT_PUSH BACK",
    "VIBER - KEPT_PARTIAL",
    "FIELD REQUEST - OTS SURE REPO",
    "FIELD REQUEST - FOR REVISIT",
    "FIELD REQUEST - BP_NC",
    "FIELD REQUEST - NEW_ADDRESS",
    "CEASE - POSSIBLE COMPLAINT",
    "CEASE - PENDING COMPLAINT",
    "CEASE - VALID COMPLAINT",
    "CEASE - REQUESTED BY BANK",
    "CEASE - CLAIMING PAID",
    "CEASE - INSURANCE CLAIM",
    "CEASE - REPOSSESSED BY OTHER ECA",
]

# Change this before you deploy! This is the password for the Admin page.
ADMIN_PASSWORD = "changeme123"

# These are always the same for every CAMS SCRAPE export — fixed, not
# related to the Frontend/Midrange/Hardcore placement on the form.
CAMS_PLACEMENT = "CURING"
CAMS_PRODUCT_TYPE = "AUTO"
CAMS_LEVEL_CYCLE = "LEVEL 2"

SUBMISSIONS_SHEET = "Submissions"
DATAGRID_SHEET = "DataGrid"
SUBMISSIONS_HEADERS = [
    "id",
    "account_number",
    "endo_date",
    "agent",
    "placement",
    "submitted_at",
    "exported",
    "exported_at",
]
DATAGRID_HEADERS = ["account_number", "uploaded_at"]

# ---- NEW: Auto Stat config (additive, does not touch anything above) ----
AUTOSTAT_SHEET = "AutoStat_Submissions"
AUTOSTAT_HEADERS = [
    "id",
    "account_number",
    "status_code",
    "remarks",
    "ptp_date",
    "ptp_amount",
    "claim_paid_date",
    "claim_paid_amount",
    "collector",
    "submitted_at",
    "exported",
    "exported_at",
]

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ----------------------------------------------------------------------
# GOOGLE SHEETS CONNECTION HELPERS
# ----------------------------------------------------------------------

@st.cache_resource
def _get_gspread_client():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=GOOGLE_SCOPES
    )
    return gspread.authorize(creds)


@st.cache_resource
def _get_spreadsheet():
    gc = _get_gspread_client()
    return gc.open_by_url(st.secrets["sheets"]["spreadsheet_url"])


@st.cache_resource
def _get_or_create_ws(name, headers_tuple):
    """Cached — only hits the Sheets API once per app lifetime, not on
    every Streamlit re-run, to stay within the free quota."""
    headers = list(headers_tuple)
    sh = _get_spreadsheet()
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=2000, cols=len(headers) + 2)
        ws.append_row(headers)
        return ws
    if not ws.row_values(1):
        ws.append_row(headers)
    return ws


def init_db():
    """Ensures all tabs exist with the right headers. Only runs once
    per session thanks to the session_state guard in main().
    NOTE: the AutoStat line below is the ONLY addition here — the two
    original lines are untouched."""
    _get_or_create_ws(SUBMISSIONS_SHEET, tuple(SUBMISSIONS_HEADERS))
    _get_or_create_ws(DATAGRID_SHEET, tuple(DATAGRID_HEADERS))
    _get_or_create_ws(AUTOSTAT_SHEET, tuple(AUTOSTAT_HEADERS))  # NEW


def normalize_account(raw) -> str:
    """Strip everything except digits, so '001-388-06608188-2' and
    '1388066081882' compare as equal."""
    if raw is None:
        return ""
    return re.sub(r"\D", "", str(raw))


def format_account_number(raw) -> str:
    """Format account number into 000-000-00000000-0 format.
    Handles raw digits, hyphenated strings, and short strings with missing leading zeros.
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    d = re.sub(r"\D", "", s)
    if not d:
        return s
    if len(d) <= 15:
        d_padded = d.zfill(15)
        return f"{d_padded[0:3]}-{d_padded[3:6]}-{d_padded[6:14]}-{d_padded[14]}"
    return s


def insert_submission(account_number, endo_date, agent, placement):
    ws = _get_or_create_ws(SUBMISSIONS_SHEET, tuple(SUBMISSIONS_HEADERS))
    new_id = uuid.uuid4().hex[:10]
    ws.append_row(
        [
            new_id,
            normalize_account(account_number),
            endo_date.isoformat(),
            agent.strip().upper(),
            placement.upper(),
            datetime.now().isoformat(timespec="seconds"),
            0,
            "",
        ],
        value_input_option="RAW",
    )


def load_submissions() -> pd.DataFrame:
    ws = _get_or_create_ws(SUBMISSIONS_SHEET, tuple(SUBMISSIONS_HEADERS))
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=SUBMISSIONS_HEADERS)
    df = pd.DataFrame(records)
    df["exported"] = pd.to_numeric(df["exported"], errors="coerce").fillna(0).astype(int)
    df["account_number"] = df["account_number"].astype(str)
    df["id"] = df["id"].astype(str)
    return df.iloc[::-1].reset_index(drop=True)  # newest submissions first


def replace_datagrid_accounts(account_numbers):
    """Wipes and reloads the DataGrid reference tab on every upload,
    since you re-download DataGrid regularly and it should always
    reflect the latest export."""
    ws = _get_or_create_ws(DATAGRID_SHEET, tuple(DATAGRID_HEADERS))
    ws.clear()
    ws.append_row(DATAGRID_HEADERS)
    now = datetime.now().isoformat(timespec="seconds")
    rows = [[acc, now] for acc in account_numbers if acc]
    if rows:
        ws.append_rows(rows, value_input_option="RAW")


def load_datagrid_set() -> set:
    """Reads raw values instead of get_all_records() on purpose:
    get_all_records() hard-fails (GSpreadException) if the DataGrid
    sheet's header row ever ends up with a blank or duplicate cell
    (e.g. from a manual edit, a partial write, or leftover columns
    from when the worksheet was created with cols=len(headers)+2).
    Reading raw values and locating 'account_number' by position is
    immune to that and degrades gracefully even with no header row."""
    ws = _get_or_create_ws(DATAGRID_SHEET, tuple(DATAGRID_HEADERS))
    all_values = ws.get_all_values()
    if not all_values:
        return set()
    header = [h.strip() for h in all_values[0]]
    try:
        acct_idx = header.index("account_number")
    except ValueError:
        acct_idx = 0  # header row missing/garbled — fall back to column A
    accounts = set()
    for row in all_values[1:]:
        if len(row) > acct_idx:
            val = str(row[acct_idx]).strip()
            if val:
                accounts.add(val)
    return accounts

def mark_exported(ids):
    """Finds the given submission ids in the Submissions tab and sets
    exported=1 + a timestamp, in a single batched write."""
    if not ids:
        return
    ids = set(str(i) for i in ids)
    ws = _get_or_create_ws(SUBMISSIONS_SHEET, tuple(SUBMISSIONS_HEADERS))
    all_values = ws.get_all_values()
    if not all_values:
        return
    header = all_values[0]
    id_col = header.index("id")
    exported_col = header.index("exported")
    exported_at_col = header.index("exported_at")
    now = datetime.now().isoformat(timespec="seconds")

    updates = []
    for sheet_row_num, row in enumerate(all_values[1:], start=2):
        if len(row) > id_col and row[id_col] in ids:
            updates.append(
                {
                    "range": gspread.utils.rowcol_to_a1(sheet_row_num, exported_col + 1),
                    "values": [[1]],
                }
            )
            updates.append(
                {
                    "range": gspread.utils.rowcol_to_a1(sheet_row_num, exported_at_col + 1),
                    "values": [[now]],
                }
            )
    if updates:
        ws.batch_update(updates, value_input_option="RAW")


# ----------------------------------------------------------------------
# EXCEL EXPORT — formatted ready for your CAMS SCRAPE paste-in columns
# ----------------------------------------------------------------------

def build_export_workbook(df: pd.DataFrame) -> bytes:
    """One sheet per placement (Frontend/Midrange/Hardcore — you log
    into a different CAMS account for each). Columns match your CAMS
    SCRAPE paste-in columns exactly: NEW ACCOUNT NUMBER (zero-padded
    15-digit), DATE (MM/DD/YYYY), AGENT, PLACEMENT, PRODUCT TYPE,
    LEVEL/CYCLE."""
    columns = ["NEW ACCOUNT NUMBER", "DATE", "AGENT", "PLACEMENT", "PRODUCT TYPE", "LEVEL/CYCLE"]

    def format_rows(sub_df):
        rows = []
        for _, r in sub_df.iterrows():
            rows.append(
                {
                    "NEW ACCOUNT NUMBER": str(r["account_number"]).zfill(15),
                    "DATE": datetime.strptime(r["endo_date"], "%Y-%m-%d").strftime("%m/%d/%Y"),
                    "AGENT": r["agent"],
                    "PLACEMENT": CAMS_PLACEMENT,
                    "PRODUCT TYPE": CAMS_PRODUCT_TYPE,
                    "LEVEL/CYCLE": CAMS_LEVEL_CYCLE,
                }
            )
        return pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for placement in PLACEMENTS:
            sub = df[df["placement"] == placement]
            out_df = format_rows(sub)
            out_df.to_excel(writer, sheet_name=placement[:31], index=False)
    return output.getvalue()


# ========================================================================
# NEW: AUTO STAT — data helpers (additive; mirrors the pattern above)
# ========================================================================

def insert_autostat_submission(
    account_number,
    status_code,
    remarks,
    ptp_date,
    ptp_amount,
    claim_paid_date,
    claim_paid_amount,
    collector,
    remark_dt=None,
):
    """Saves one Auto Stat status-update row. ptp_date / claim_paid_date
    are `date` objects or None. ptp_amount / claim_paid_amount are
    numbers or None. remark_dt is the agent-entered remark datetime."""
    ws = _get_or_create_ws(AUTOSTAT_SHEET, tuple(AUTOSTAT_HEADERS))
    new_id = uuid.uuid4().hex[:10]
    submitted_at = remark_dt.isoformat(timespec="seconds") if remark_dt else datetime.now().isoformat(timespec="seconds")
    ws.append_row(
        [
            new_id,
            format_account_number(account_number),
            status_code.strip(),
            remarks.strip(),
            ptp_date.isoformat() if ptp_date else "",
            ptp_amount if ptp_amount not in (None, "") else "",
            claim_paid_date.isoformat() if claim_paid_date else "",
            claim_paid_amount if claim_paid_amount not in (None, "") else "",
            collector.strip().upper(),
            submitted_at,
            0,
            "",
        ],
        value_input_option="RAW",
    )


def load_autostat_submissions() -> pd.DataFrame:
    ws = _get_or_create_ws(AUTOSTAT_SHEET, tuple(AUTOSTAT_HEADERS))
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=AUTOSTAT_HEADERS)
    df = pd.DataFrame(records)
    df["exported"] = pd.to_numeric(df["exported"], errors="coerce").fillna(0).astype(int)
    df["account_number"] = df["account_number"].astype(str)
    df["id"] = df["id"].astype(str)
    return df.iloc[::-1].reset_index(drop=True)  # newest first


def mark_autostat_exported(ids):
    """Same pattern as mark_exported(), scoped to the AutoStat_Submissions
    tab only."""
    if not ids:
        return
    ids = set(str(i) for i in ids)
    ws = _get_or_create_ws(AUTOSTAT_SHEET, tuple(AUTOSTAT_HEADERS))
    all_values = ws.get_all_values()
    if not all_values:
        return
    header = all_values[0]
    id_col = header.index("id")
    exported_col = header.index("exported")
    exported_at_col = header.index("exported_at")
    now = datetime.now().isoformat(timespec="seconds")

    updates = []
    for sheet_row_num, row in enumerate(all_values[1:], start=2):
        if len(row) > id_col and row[id_col] in ids:
            updates.append(
                {
                    "range": gspread.utils.rowcol_to_a1(sheet_row_num, exported_col + 1),
                    "values": [[1]],
                }
            )
            updates.append(
                {
                    "range": gspread.utils.rowcol_to_a1(sheet_row_num, exported_at_col + 1),
                    "values": [[now]],
                }
            )
    if updates:
        ws.batch_update(updates, value_input_option="RAW")


def clear_autostat_submissions():
    """Wipes all data rows from AutoStat_Submissions (keeps the header)."""
    ws = _get_or_create_ws(AUTOSTAT_SHEET, tuple(AUTOSTAT_HEADERS))
    ws.clear()
    ws.append_row(AUTOSTAT_HEADERS)



def _parse_amount(val):
    """Best-effort numeric parse; blanks/None/nan -> None."""
    if val in (None, "", "None", "nan", "NaN") or pd.isnull(val):
        return None
    try:
        n = float(val)
        return None if math.isnan(n) or math.isinf(n) else n
    except (TypeError, ValueError):
        return None


def build_autostat_export_workbook(df: pd.DataFrame):
    """Takes the filtered Auto Stat submissions and produces the final
    two-sheet workbook, per Urban's manual process:

      1. Dedupe fix: for repeat account numbers, bump REMARKS DATE by
         +1 minute per repeat (non-destructive — only in this export copy).
      2. Segregate: rows with a Claim Paid Date/Amount go to their own
         "CLAIM PAID" sheet and are excluded from "CLEAN".

    Returns (workbook_bytes, clean_count, claim_paid_count).
    """
    work = df.copy()

    # Parse submitted_at into real datetimes to allow the +1 min bump.
    work["_remarks_dt"] = pd.to_datetime(work["submitted_at"], errors="coerce")

    # --- Step 1: duplicate account number -> +1 min per repeat ---------
    work = work.sort_values(["account_number", "_remarks_dt"], kind="stable")
    work["_dupe_rank"] = work.groupby("account_number").cumcount()  # 0,1,2...
    work["_remarks_dt"] = work.apply(
        lambda r: r["_remarks_dt"] + pd.Timedelta(minutes=r["_dupe_rank"])
        if pd.notnull(r["_remarks_dt"])
        else r["_remarks_dt"],
        axis=1,
    )

    def _is_valid_val(v):
        if v is None or pd.isnull(v):
            return False
        s = str(v).strip().lower()
        return bool(s) and s not in ("none", "nan", "nat", "<na>", "null", "")

    # --- Step 2: split on Claim Paid Date / Claim Paid Amount ----------
    def _has_claim(row):
        cd = row.get("claim_paid_date")
        ca = _parse_amount(row.get("claim_paid_amount"))
        sc = str(row.get("status_code", "") or "").upper()
        return _is_valid_val(cd) or (ca is not None and ca != 0) or ("KEPT" in sc)

    work["_has_claim"] = work.apply(_has_claim, axis=1)

    clean_rows = work[~work["_has_claim"]]
    claim_rows = work[work["_has_claim"]]

    def _fmt_remarks_date(dt):
        if pd.isnull(dt):
            return ""
        return dt.strftime("%m/%d/%Y %H:%M:%S")

    def _fmt_ptp_date(val):
        if not _is_valid_val(val):
            return ""
        s = str(val).strip()
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%m/%d/%Y")
        except ValueError:
            return s

    def _fmt_amount(val):
        n = _parse_amount(val)
        return round(n, 2) if n is not None else ""

    def _fmt_account(raw):
        return format_account_number(raw)

    clean_cols = [
        "NEW ACCOUNT NUMBER",
        "STATUS CODE",
        "REMARKS",
        "REMARKS DATE",
        "PTP DATE",
        "PTP AMOUNT",
        "COLLECTOR",
    ]
    claim_cols = clean_cols + ["CLAIM PAID DATE", "CLAIM PAID AMOUNT"]

    def _build_clean(sub):
        rows = []
        for _, r in sub.iterrows():
            rows.append(
                {
                    "NEW ACCOUNT NUMBER": _fmt_account(r["account_number"]),
                    "STATUS CODE": r["status_code"],
                    "REMARKS": r["remarks"],
                    "REMARKS DATE": _fmt_remarks_date(r["_remarks_dt"]),
                    "PTP DATE": _fmt_ptp_date(r.get("ptp_date")),
                    "PTP AMOUNT": _fmt_amount(r.get("ptp_amount")),
                    "COLLECTOR": str(r["collector"]).upper(),
                }
            )
        return pd.DataFrame(rows, columns=clean_cols) if rows else pd.DataFrame(columns=clean_cols)

    def _build_claim(sub):
        rows = []
        for _, r in sub.iterrows():
            sc = str(r.get("status_code", "") or "").upper()
            cd = r.get("claim_paid_date") if _is_valid_val(r.get("claim_paid_date")) else (r.get("ptp_date") if "KEPT" in sc else "")
            ca = r.get("claim_paid_amount") if _parse_amount(r.get("claim_paid_amount")) is not None else (r.get("ptp_amount") if "KEPT" in sc else "")
            p_date = "" if "KEPT" in sc else r.get("ptp_date")
            p_amt = "" if "KEPT" in sc else r.get("ptp_amount")

            rows.append(
                {
                    "NEW ACCOUNT NUMBER": _fmt_account(r["account_number"]),
                    "STATUS CODE": r["status_code"],
                    "REMARKS": r["remarks"],
                    "REMARKS DATE": _fmt_remarks_date(r["_remarks_dt"]),
                    "PTP DATE": _fmt_ptp_date(p_date),
                    "PTP AMOUNT": _fmt_amount(p_amt),
                    "COLLECTOR": str(r["collector"]).upper(),
                    "CLAIM PAID DATE": _fmt_ptp_date(cd),
                    "CLAIM PAID AMOUNT": _fmt_amount(ca),
                }
            )
        return pd.DataFrame(rows, columns=claim_cols) if rows else pd.DataFrame(columns=claim_cols)

    clean_df = _build_clean(clean_rows)
    claim_df = _build_claim(claim_rows)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        clean_df.to_excel(writer, sheet_name="CLEAN", index=False)
        claim_df.to_excel(writer, sheet_name="CLAIM PAID", index=False)

    return output.getvalue(), len(clean_df), len(claim_df), claim_df


def build_claim_paid_only_workbook(df: pd.DataFrame):
    """Single-sheet Excel with ONLY the CLAIM PAID rows.
    Reuses build_autostat_export_workbook's splitting logic.
    Returns (workbook_bytes, claim_paid_count).
    """
    _, _, _, claim_df = build_autostat_export_workbook(df)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        claim_df.to_excel(writer, sheet_name="CLAIM PAID", index=False)
    return output.getvalue(), len(claim_df)


# ========================================================================
# BULK PASTE — helpers
# ========================================================================

def _parse_date_flex(val):
    """Accept YYYY/MM/DD, MM/DD/YYYY, YYYY-MM-DD, MM/DD/YY, or Timestamp/datetime objects."""
    if val is None or pd.isnull(val):
        return None
    if isinstance(val, (datetime, date, pd.Timestamp)):
        try:
            return val.date() if hasattr(val, "date") else val
        except Exception:
            pass
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ("nan", "nat", "none", ""):
        return None
    val_str = val_str.split()[0]  # drop any time component
    for fmt in ["%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"]:
        try:
            return datetime.strptime(val_str, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount_str(val):
    """'30,420.00' or '30420' -> float or None."""
    if val in (None, ""):
        return None
    cleaned = re.sub(r"[^\d.]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_pasted_tsv(text):
    """Split tab-separated paste into list of rows (each row = list of str).
    Uses csv reader so quoted multi-line cells are handled correctly."""
    import csv, io
    reader = csv.reader(io.StringIO(text.strip()), delimiter="\t")
    return [row for row in reader if any(c.strip() for c in row)]


DT_RE = re.compile(r"(\d{4}/\d{2}/\d{2})\s+(\d{1,2}:\d{2})")


def _parse_lark_autostat_paste(text):
    """Parse a Lark AUTOSTAT form export where REMARKS spans multiple lines.

    Lark record structure (tabs between columns, newlines inside remarks):
      LINE 1 : acct_no \t submitted_on \t respondent \t status_code \t [remarks_start]
      LINES 2+: [remarks continuation, no tabs]
      LAST LINE: [remarks_end] \t ptp_date \t ptp_amount \t remark_timestamp \t \t \t collector

    Identifies record boundaries by:
    - Record START: first column normalises to >=10 digits (account number)
    - Record END:   a line containing a YYYY/MM/DD HH:MM timestamp (remark date)
    """
    lines = text.strip().split("\n")
    records = []
    current = None

    for line in lines:
        parts = line.split("\t")
        first_col = parts[0].strip()

        # ---- Record start detection ----
        # Must have at least 4 tab-separated columns AND first col normalises to an account number.
        # This prevents phone numbers inside remarks (e.g. "MOBILE/ Landline: 09954305771")
        # from being mistaken for a record start — they have no tabs so len(parts)==1.
        if len(parts) >= 4 and len(normalize_account(first_col)) >= 10:
            if current is not None:
                records.append(current)
            current = {
                "account":     first_col,
                "status_code": parts[3].strip() if len(parts) > 3 else "",
                "remarks_lines": ["	".join(parts[4:]).strip()] if len(parts) > 4 else [],
                "ptp_date":    "",
                "ptp_amount":  "",
                "remark_date": "",
                "claim_paid_date": "",
                "claim_paid_amount": "",
                "collector":   "",
            }
            continue

        if current is None:
            continue  # stray line before any record

        # ---- Record end detection: line contains a YYYY/MM/DD HH:MM timestamp ----
        ts_match = DT_RE.search(line)
        if ts_match:
            ts_idx = None
            for j, p in enumerate(parts):
                if DT_RE.search(p):
                    ts_idx = j
                    break

            # Parts BEFORE timestamp (minus 2 for ptp_date, ptp_amount)
            if ts_idx is not None and ts_idx >= 2:
                remarks_tail = "\t".join(parts[:ts_idx - 2]).strip()
                if remarks_tail:
                    current["remarks_lines"].append(remarks_tail)
                current["ptp_date"]    = parts[ts_idx - 2].strip()
                current["ptp_amount"]  = parts[ts_idx - 1].strip()
                current["remark_date"] = parts[ts_idx].strip()
                # Claim paid date/amount follow timestamp (if present)
                after = [p.strip() for p in parts[ts_idx + 1:]]
                # after = [claim_paid_date, claim_paid_amount, ..., collector]
                # collector = last non-empty
                non_empty = [(i, v) for i, v in enumerate(after) if v]
                if non_empty:
                    current["collector"] = non_empty[-1][1]
                    # If there are 3+ non-empty after ts, first two are claim paid date/amount
                    if len(non_empty) >= 3:
                        current["claim_paid_date"]   = non_empty[0][1]
                        current["claim_paid_amount"] = non_empty[1][1]
            else:
                current["remarks_lines"].append(line.strip())
        else:
            # Continuation remarks line
            current["remarks_lines"].append(line.strip())

    if current is not None:
        records.append(current)

    return records


def bulk_insert_submissions(rows):
    """rows: list of dicts with keys: account_number, endo_date (str ISO),
    agent, placement."""
    ws = _get_or_create_ws(SUBMISSIONS_SHEET, tuple(SUBMISSIONS_HEADERS))
    now = datetime.now().isoformat(timespec="seconds")
    sheet_rows = [
        [
            uuid.uuid4().hex[:10],
            normalize_account(r["account_number"]),
            r["endo_date"],
            r["agent"].strip().upper(),
            r["placement"].strip().upper(),
            now,
            0,
            "",
        ]
        for r in rows
    ]
    if sheet_rows:
        ws.append_rows(sheet_rows, value_input_option="RAW")


def _json_safe_val(v):
    if v is None or pd.isnull(v):
        return ""
    s = str(v)
    if s.strip().lower() in ("nan", "nat", "<na>", "none"):
        return ""
    if isinstance(v, (np.integer, int)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        if np.isnan(v) or np.isinf(v):
            return ""
        return float(v)
    return s


def bulk_insert_autostat_submissions(rows):
    """rows: list of dicts with keys matching AUTOSTAT_HEADERS fields."""
    ws = _get_or_create_ws(AUTOSTAT_SHEET, tuple(AUTOSTAT_HEADERS))
    sheet_rows = []
    for r in rows:
        sc = str(r.get("status_code", "") or "").strip()
        p_date = str(r.get("ptp_date", "") or "").strip()
        p_amt = r.get("ptp_amount", "")
        c_date = str(r.get("claim_paid_date", "") or "").strip()
        c_amt = r.get("claim_paid_amount", "")

        # If status is KEPT and claim_paid_date is empty, shift ptp_date -> claim_paid_date
        if "KEPT" in sc.upper():
            if not c_date and p_date:
                c_date = p_date
                p_date = ""
            if (c_amt in (None, "", "None") or pd.isnull(c_amt)) and (p_amt not in (None, "", "None") and pd.notnull(p_amt)):
                c_amt = p_amt
                p_amt = ""

        row_vals = [
            uuid.uuid4().hex[:10],
            format_account_number(str(r.get("account_number", ""))),
            sc,
            str(r.get("remarks", "") or "").strip(),
            p_date,
            float(p_amt) if p_amt not in (None, "", "None") and pd.notnull(p_amt) and _parse_amount(p_amt) is not None else "",
            c_date,
            float(c_amt) if c_amt not in (None, "", "None") and pd.notnull(c_amt) and _parse_amount(c_amt) is not None else "",
            str(r.get("collector", "") or "").strip().upper(),
            str(r.get("submitted_at", datetime.now().isoformat(timespec="seconds")) or "").strip(),
            0,
            "",
        ]
        clean_row = [_json_safe_val(v) for v in row_vals]
        sheet_rows.append(clean_row)

    if sheet_rows:
        ws.append_rows(sheet_rows, value_input_option="RAW")


# ========================================================================
# BULK PASTE — UI sections (called from each admin dashboard)
# ========================================================================

def bulk_paste_endo_section():
    """Admin pastes raw Endo rows (tab-separated) for batch import.

    Expected column order (matching your spreadsheet export):
      0: Account Number
      1: Endo Date  (any of YYYY/MM/DD, MM/DD/YYYY, YYYY-MM-DD)
      2: Agent / CMS username
      3: (ignored — leave blank or any value)
      4: Placement  (FRONTEND / MIDRANGE / HARDCORE)
    """
    st.subheader("⑤ Bulk Paste — Endo Import")
    st.caption(
        "Paste rows directly from your spreadsheet (tab-separated). "
        "Expected columns: **Account Number | Endo Date | Agent | (ignored) | Placement**"
    )
    raw = st.text_area(
        "Paste rows here (one account per line)",
        height=180,
        placeholder="1968062853581\t08/20/2026\tCSAYSON\t\tFRONTEND",
        key="bulk_endo_paste",
    )

    if not raw.strip():
        return

    parsed, errors = [], []
    for i, row in enumerate(_parse_pasted_tsv(raw), 1):
        # Pad short rows
        while len(row) < 5:
            row.append("")
        acct = normalize_account(row[0])
        d = _parse_date_flex(row[1])
        agent = row[2].strip()
        placement = row[4].strip().upper()
        if len(acct) < 10:
            errors.append(f"Row {i}: invalid account number '{row[0]}'")
            continue
        if not d:
            errors.append(f"Row {i}: can't parse date '{row[1]}'")
            continue
        if not agent:
            errors.append(f"Row {i}: agent is blank")
            continue
        if placement not in PLACEMENTS:
            errors.append(f"Row {i}: placement '{row[4]}' not in {PLACEMENTS}")
            continue
        parsed.append({"account_number": acct, "endo_date": d.isoformat(),
                       "agent": agent, "placement": placement})

    if errors:
        for e in errors:
            st.warning(e)

    if parsed:
        st.caption(f"Preview — **{len(parsed)}** row(s) ready to import:")
        st.dataframe(pd.DataFrame(parsed), use_container_width=True, hide_index=True)
        if st.button(f"✅ Confirm import {len(parsed)} Endo row(s)", key="confirm_bulk_endo"):
            bulk_insert_submissions(parsed)
            st.success(f"Imported {len(parsed)} row(s) successfully!")
            st.rerun()


def excel_upload_autostat_section():
    """Upload the Lark AUTOSTAT FORM Excel export directly.
    Compares against existing DB rows using (account_number + remark_date)
    so only NEW rows are imported.
    """
    st.subheader("② Upload Excel — Auto Stat Import")
    st.caption(
        "Upload your **PSB NEW ENDO PROCESS_AUTOSTAT FORM_All Results.xlsx** file. "
        "Rows already in the database (matched by Account + Remark Date) are skipped automatically."
    )

    # --- Danger zone: clear all data ---
    with st.expander("🗑️ Clear all AutoStat data (test data reset)", expanded=False):
        st.warning("This will **permanently delete** all rows in the AutoStat database. Use only to clear test data.")
        if "confirm_clear_as" not in st.session_state:
            st.session_state["confirm_clear_as"] = False
        if st.button("I understand — Clear AutoStat Database", key="clear_as_btn"):
            st.session_state["confirm_clear_as"] = True
        if st.session_state.get("confirm_clear_as"):
            if st.button("✅ YES, delete everything", key="clear_as_confirm"):
                clear_autostat_submissions()
                st.session_state["confirm_clear_as"] = False
                st.success("Database cleared. You may now upload the Excel file.")
                st.rerun()

    xfile = st.file_uploader(
        "AUTOSTAT FORM Excel (.xlsx)",
        type=["xlsx"],
        key="excel_autostat_upload",
    )
    if not xfile:
        return

    try:
        xdf = pd.read_excel(xfile)
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return

    # --- Column mapping (case-insensitive) ---
    xdf.columns = [str(c).strip() for c in xdf.columns]
    col_map = {
        "ACCOUNT NUMBER":    "account_number",
        "STATUS CODE":       "status_code",
        "REMARKS":           "remarks",
        "PTP DATE":          "ptp_date",
        "PTP AMOUNT":        "ptp_amount",
        "REMARK DATE":       "remark_date",
        "CLAIM PAID DATE":   "claim_paid_date",
        "CLAIM PAID AMOUNT": "claim_paid_amount",
        "CMS USERNAME":      "collector",
    }
    missing = [c for c in col_map if c not in xdf.columns]
    if missing:
        st.error(f"Missing columns in Excel: {missing}")
        return

    xdf = xdf.rename(columns=col_map)

    # --- Load existing DB keys: set of (normalized_account, remark_date_iso) ---
    existing_df = load_autostat_submissions()
    existing_keys = set()
    for _, row in existing_df.iterrows():
        acct = normalize_account(str(row["account_number"]))
        sat  = str(row.get("submitted_at", "")).strip()[:16]  # YYYY-MM-DDTHH:MM
        existing_keys.add((acct, sat))

    # --- Parse + dedupe ---
    new_rows, skipped = [], 0
    for _, r in xdf.iterrows():
        acct = normalize_account(str(r["account_number"]))
        if len(acct) < 10:
            skipped += 1
            continue

        # Parse remark date
        rd = r["remark_date"]
        if pd.isnull(rd):
            remark_dt = None
            remark_iso = ""
        elif isinstance(rd, (pd.Timestamp, datetime)):
            remark_dt = pd.Timestamp(rd).to_pydatetime()
            remark_iso = remark_dt.strftime("%Y-%m-%dT%H:%M")
        else:
            try:
                remark_dt = datetime.strptime(str(rd).split("(")[0].strip(), "%Y/%m/%d %H:%M")
                remark_iso = remark_dt.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                remark_dt = None
                remark_iso = str(rd)[:16]

        key = (acct, remark_iso)
        if key in existing_keys:
            skipped += 1
            continue

        # Parse dates & amounts using _parse_date_flex
        ptp_d = _parse_date_flex(r.get("ptp_date"))
        ptp_date_str = ptp_d.strftime("%Y-%m-%d") if ptp_d else ""

        cpd_d = _parse_date_flex(r.get("claim_paid_date"))
        claim_date_str = cpd_d.strftime("%Y-%m-%d") if cpd_d else ""

        ptp_amt  = _parse_amount(r.get("ptp_amount"))
        claim_amt = _parse_amount(r.get("claim_paid_amount"))
        collector = str(r["collector"]).strip().upper() if pd.notna(r.get("collector")) else ""
        status_code = str(r["status_code"]).strip() if pd.notna(r.get("status_code")) else ""
        remarks = str(r["remarks"]).strip() if pd.notna(r.get("remarks")) else ""

        # Handle KEPT status: if claim date/amount is in PTP columns in Lark export, move to claim columns
        if "KEPT" in status_code.upper():
            if not claim_date_str and ptp_date_str:
                claim_date_str = ptp_date_str
                ptp_date_str = ""
            if claim_amt is None and ptp_amt is not None:
                claim_amt = ptp_amt
                ptp_amt = None

        submitted_at = remark_dt.isoformat(timespec="seconds") if remark_dt else datetime.now().isoformat(timespec="seconds")

        new_rows.append({
            "account_number":   acct,
            "status_code":      status_code,
            "remarks":          remarks,
            "ptp_date":         ptp_date_str,
            "ptp_amount":       round(ptp_amt, 2) if ptp_amt is not None else "",
            "claim_paid_date":  claim_date_str,
            "claim_paid_amount":round(claim_amt, 2) if claim_amt is not None else "",
            "collector":        collector,
            "submitted_at":     submitted_at,
        })

    st.info(f"Found **{len(new_rows)}** new row(s) to import, **{skipped}** already in DB / invalid skipped.")

    if new_rows:
        preview_df = pd.DataFrame(new_rows)[[
            "account_number", "status_code", "collector",
            "ptp_date", "ptp_amount", "submitted_at",
        ]]
        st.dataframe(preview_df, use_container_width=True, hide_index=True)
        if st.button(f"✅ Import {len(new_rows)} new row(s) from Excel", key="confirm_excel_autostat"):
            bulk_insert_autostat_submissions(new_rows)
            st.success(f"Imported {len(new_rows)} row(s) successfully!")
            st.rerun()



def bulk_paste_autostat_section():
    """Admin pastes Lark AUTOSTAT form rows for batch import.
    Handles multiline REMARKS fields correctly.
    """
    st.subheader("③ Bulk Paste — Auto Stat Import")
    st.caption(
        "Paste rows directly from your Lark AUTOSTAT form export. "
        "Multiline remarks are handled automatically."
    )
    raw = st.text_area(
        "Paste rows here",
        height=220,
        placeholder="001-388-...\t2026/08/20\tShamira Tupas\tCALL - PTP FULL UPDATE\t[remarks]\t08/20/2026\t30,420.00\t2026/08/23 17:40 (GMT+8)\t\t\tSTUPAS",
        key="bulk_autostat_paste",
    )

    if not raw.strip():
        return

    records = _parse_lark_autostat_paste(raw)
    parsed, errors = [], []

    for i, rec in enumerate(records, 1):
        acct = normalize_account(rec["account"])
        status_code = rec["status_code"]
        remarks = "\n".join(r for r in rec["remarks_lines"] if r)
        ptp_date = _parse_date_flex(rec["ptp_date"])
        ptp_amount = _parse_amount_str(rec["ptp_amount"])
        claim_paid_date = _parse_date_flex(rec["claim_paid_date"])
        claim_paid_amount = _parse_amount_str(rec["claim_paid_amount"])
        collector = rec["collector"]

        # Parse remark date timestamp
        rd_raw = rec["remark_date"]
        try:
            submitted_at = datetime.strptime(
                rd_raw.split("(")[0].strip(), "%Y/%m/%d %H:%M"
            ).isoformat(timespec="seconds")
        except ValueError:
            submitted_at = datetime.now().isoformat(timespec="seconds")

        if len(acct) < 10:
            errors.append(f"Record {i}: invalid account number '{rec['account']}'")
            continue
        if not status_code:
            errors.append(f"Record {i}: status code is blank")
            continue
        if not collector:
            errors.append(f"Record {i}: collector/CMS username is blank")
            continue

        parsed.append({
            "account_number":   acct,
            "status_code":      status_code,
            "remarks":          remarks,
            "ptp_date":         ptp_date.isoformat() if ptp_date else "",
            "ptp_amount":       round(ptp_amount, 2) if ptp_amount is not None else "",
            "claim_paid_date":  claim_paid_date.isoformat() if claim_paid_date else "",
            "claim_paid_amount":round(claim_paid_amount, 2) if claim_paid_amount is not None else "",
            "collector":        collector.upper(),
            "submitted_at":     submitted_at,
        })

    if errors:
        for e in errors:
            st.warning(e)

    if parsed:
        preview_df = pd.DataFrame(parsed)[[
            "account_number", "status_code", "collector",
            "ptp_date", "ptp_amount", "claim_paid_date", "claim_paid_amount",
        ]]
        st.caption(f"Preview — **{len(parsed)}** record(s) ready to import:")
        st.dataframe(preview_df, use_container_width=True, hide_index=True)
        if st.button(f"✅ Confirm import {len(parsed)} Auto Stat row(s)", key="confirm_bulk_autostat"):
            bulk_insert_autostat_submissions(parsed)
            st.success(f"Imported {len(parsed)} row(s) successfully!")
            st.rerun()



# ----------------------------------------------------------------------
# UI — STYLES
# ----------------------------------------------------------------------

def inject_styles():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

        html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

        /* Hide default Streamlit chrome */
        #MainMenu, footer, header { visibility: hidden; }
        .stDeployButton { display: none; }
        [data-testid="stSidebar"] { display: none; }

        /* Page background */
        .stApp { background: #0a0e1a; }
        [data-testid="stAppViewContainer"] { background: #0a0e1a; }

        /* Main content padding */
        .block-container {
            padding-top: 2rem !important;
            padding-left: 3rem !important;
            padding-right: 3rem !important;
        }

        /* Hero logo banner */
        .psb-hero {
            text-align: center;
            padding: 2.5rem 2rem 2rem;
            margin-bottom: 1.5rem;
        }
        .psb-logo-btn {
            background: none;
            border: none;
            cursor: pointer;
            display: inline-block;
            padding: 0;
            margin: 0 auto 0.75rem;
        }
        .psb-badge {
            display: inline-block;
            background: linear-gradient(135deg, #c8102e 0%, #8b0000 100%);
            color: white;
            font-size: 1.05rem;
            font-weight: 800;
            letter-spacing: 0.18em;
            padding: 0.55rem 1.4rem;
            border-radius: 8px;
            box-shadow: 0 4px 24px rgba(200,16,46,0.35);
            user-select: none;
        }
        .psb-title {
            color: #ffffff;
            font-size: 1.55rem;
            font-weight: 700;
            margin: 0.5rem 0 0.2rem;
            letter-spacing: 0.02em;
        }
        .psb-sub {
            color: #6b7280;
            font-size: 0.82rem;
            font-weight: 400;
            letter-spacing: 0.06em;
            text-transform: uppercase;
        }

        /* Card */
        .psb-card {
            background: #131929;
            border: 1px solid #1e2d45;
            border-radius: 16px;
            padding: 2rem;
            margin-bottom: 1.5rem;
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
        }

        /* Form inputs */
        [data-testid="stTextInput"] input,
        [data-testid="stDateInput"] input,
        [data-testid="stNumberInput"] input,
        [data-testid="stTextArea"] textarea,
        [data-testid="stSelectbox"] > div {
            background: #0d1525 !important;
            border: 1px solid #1e2d45 !important;
            border-radius: 10px !important;
            color: #e5e7eb !important;
            font-family: 'Inter', sans-serif !important;
        }
        [data-testid="stTextInput"] input:focus {
            border-color: #c8102e !important;
            box-shadow: 0 0 0 2px rgba(200,16,46,0.2) !important;
        }
        label { color: #9ca3af !important; font-size: 0.82rem !important; font-weight: 500 !important; letter-spacing: 0.04em !important; }

        /* Submit button */
        [data-testid="stFormSubmitButton"] > button {
            width: 100% !important;
            background: linear-gradient(135deg, #c8102e, #8b0000) !important;
            color: white !important;
            border: none !important;
            border-radius: 10px !important;
            font-weight: 700 !important;
            font-size: 1rem !important;
            padding: 0.75rem !important;
            letter-spacing: 0.04em !important;
            transition: all 0.2s !important;
            box-shadow: 0 4px 16px rgba(200,16,46,0.3) !important;
            margin-top: 0.5rem !important;
        }
        [data-testid="stFormSubmitButton"] > button:hover {
            transform: translateY(-1px) !important;
            box-shadow: 0 6px 24px rgba(200,16,46,0.45) !important;
        }

        /* Admin button (generic) */
        .stButton > button {
            background: #1e2d45 !important;
            color: #e5e7eb !important;
            border: 1px solid #2d3f5a !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
        }
        .stButton > button:hover {
            background: #253654 !important;
            border-color: #c8102e !important;
        }

        /* Download button */
        [data-testid="stDownloadButton"] > button {
            background: linear-gradient(135deg, #166534, #14532d) !important;
            color: white !important;
            border: none !important;
            border-radius: 8px !important;
            font-weight: 600 !important;
            box-shadow: 0 4px 12px rgba(22,101,52,0.3) !important;
        }

        /* Secret admin trigger — invisible tap zone on logo */
        .secret-trigger button {
            background: transparent !important;
            border: none !important;
            color: transparent !important;
            font-size: 1px !important;
            padding: 0 !important;
            min-height: 0 !important;
            height: 0px !important;
            width: 100% !important;
            cursor: default !important;
            box-shadow: none !important;
        }

        /* Divider */
        hr { border-color: #1e2d45 !important; }

        /* Metrics */
        [data-testid="stMetric"] { background: #131929; border: 1px solid #1e2d45; border-radius: 10px; padding: 1rem; }
        [data-testid="stMetricLabel"] { color: #6b7280 !important; }
        [data-testid="stMetricValue"] { color: #e5e7eb !important; }

        /* Success / error / warning */
        [data-testid="stAlert"] { border-radius: 10px !important; }

        /* NEW: process picker cards (agent + admin) */
        .process-pick-btn button {
            width: 100% !important;
            padding: 1.75rem 1rem !important;
            font-size: 1.05rem !important;
            border-radius: 14px !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ----------------------------------------------------------------------
# UI — AGENT FORM (Endo) — UNCHANGED FROM ORIGINAL
# ----------------------------------------------------------------------

def agent_form_page():
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("""
            <div class="psb-hero">
                <div class="psb-badge">PSB</div>
                <div class="psb-title">NEW ENDO PROCESS</div>
                <div class="psb-sub">Auto Loan Curing · Agent Submission</div>
            </div>
        """, unsafe_allow_html=True)

        with st.form("endo_form", clear_on_submit=True):
            account_number = st.text_input(
                "ACCOUNT NUMBER",
                placeholder="e.g. 001-388-06387719-6 or digits only",
            )
            endo_date = st.date_input("ENDO DATE", value=date.today())
            agent = st.text_input("AGENT", placeholder="CMS username e.g. CSAYSON")
            placement = st.selectbox("PLACEMENT", PLACEMENTS)

            submitted = st.form_submit_button("✦ SUBMIT ENDO")

            if submitted:
                errors = []
                clean_account = normalize_account(account_number)
                if not clean_account or len(clean_account) < 10:
                    errors.append("Account Number looks invalid — enter at least 10 digits.")
                if not agent.strip():
                    errors.append("Agent is required.")

                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    insert_submission(clean_account, endo_date, agent, placement)
                    st.success("✅ Submitted successfully! You may submit another one.")


# ========================================================================
# NEW: UI — AGENT FORM (Auto Stat)
# ========================================================================

def autostat_form_page():
    # Version counter — incrementing it resets all widget keys (clears form)
    if "as_v" not in st.session_state:
        st.session_state["as_v"] = 0
    v = st.session_state["as_v"]

    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("""
            <div class="psb-hero">
                <div class="psb-badge">PSB</div>
                <div class="psb-title">AUTO STAT</div>
                <div class="psb-sub">Auto Loan Curing · Status Update Submission</div>
            </div>
        """, unsafe_allow_html=True)

        account_number = st.text_input(
            "ACCOUNT NUMBER",
            placeholder="e.g. 001-388-06387719-6 or digits only",
            key=f"as_acct_{v}",
        )

        status_code = st.selectbox(
            "STATUS CODE",
            STATUS_CODES,
            index=None,
            placeholder="Type to search status...",
            key=f"as_sc_{v}",
        )

        # — Conditional fields based on status code (placed before Remarks) —
        ptp_date = None
        ptp_amount = None
        claim_paid_date = None
        claim_paid_amount = None

        sc = status_code or ""
        has_ptp  = "PTP"  in sc.upper()
        has_kept = "KEPT" in sc.upper()

        if has_ptp:
            st.caption("📅 PTP details required for this status:")
            p1, p2 = st.columns(2)
            with p1:
                ptp_date = st.date_input("PTP DATE", value=None, key=f"as_pd_{v}")
            with p2:
                ptp_amount = st.number_input(
                    "PTP AMOUNT", min_value=0.0, step=0.01, format="%.2f", key=f"as_pa_{v}"
                )

        if has_kept:
            st.caption("💳 Claim details required for this status:")
            cp1, cp2 = st.columns(2)
            with cp1:
                claim_paid_date = st.date_input("CLAIM PAID DATE", value=None, key=f"as_cpd_{v}")
            with cp2:
                claim_paid_amount = st.number_input(
                    "CLAIM PAID AMOUNT", min_value=0.0, step=0.01, format="%.2f", key=f"as_cpa_{v}"
                )

        remarks = st.text_area(
            "REMARKS",
            placeholder="Full remarks / spiel notes",
            key=f"as_rem_{v}",
        )

        # Remark Date + Time (matches Lark form)
        rd_col1, rd_col2 = st.columns(2)
        with rd_col1:
            remark_date = st.date_input("REMARK DATE", value=date.today(), key=f"as_rd_{v}")
        with rd_col2:
            remark_time = st.time_input(
                "REMARK TIME",
                value=datetime.now().time().replace(second=0, microsecond=0),
                step=60,          # 1-minute steps — agents can type exact time e.g. 08:52
                key=f"as_rt_{v}",
            )

        collector = st.text_input(
            "CMS USERNAME",
            placeholder="CAPSLOCK PLEASE e.g. CSAYSON",
            key=f"as_col_{v}",
        )

        if st.button("✦ SUBMIT STATUS", use_container_width=True, key=f"as_sub_{v}"):
            errors = []
            clean_account = normalize_account(account_number)
            if not clean_account or len(clean_account) < 10:
                errors.append("Account Number looks invalid — enter at least 10 digits.")
            if not status_code:
                errors.append("Status Code is required.")
            if not collector.strip():
                errors.append("CMS Username is required.")
            if has_ptp and not ptp_date:
                errors.append("PTP Date is required for this status.")

            if errors:
                for e in errors:
                    st.error(e)
            else:
                # Build combined submitted_at from remark date + time
                remark_dt = datetime.combine(remark_date, remark_time)
                insert_autostat_submission(
                    clean_account,
                    status_code,
                    remarks,
                    ptp_date,
                    ptp_amount if (has_ptp and ptp_amount) else None,
                    claim_paid_date,
                    claim_paid_amount if (has_kept and claim_paid_amount) else None,
                    collector,
                    remark_dt=remark_dt,
                )
                st.success("✅ Submitted! You may submit another one.")
                st.session_state["as_v"] += 1
                st.rerun()



# ========================================================================
# NEW: UI — PROCESS PICKER (agent landing page)
# ========================================================================

def process_picker_page():
    _, col, _ = st.columns([1, 2, 1])
    with col:
        st.markdown("""
            <div class="psb-hero">
                <div class="psb-badge">PSB</div>
                <div class="psb-title">CHOOSE A PROCESS</div>
                <div class="psb-sub">Auto Loan Curing</div>
            </div>
        """, unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown('<div class="process-pick-btn">', unsafe_allow_html=True)
            if st.button("🧾 New Endo", use_container_width=True):
                st.session_state["agent_process"] = "endo"
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        with c2:
            st.markdown('<div class="process-pick-btn">', unsafe_allow_html=True)
            if st.button("📊 Auto Stat", use_container_width=True):
                st.session_state["agent_process"] = "autostat"
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)


# ----------------------------------------------------------------------
# UI — ADMIN LOGIN — UNCHANGED FROM ORIGINAL
# ----------------------------------------------------------------------

def admin_login():
    st.markdown("""
        <div class="psb-hero">
            <div class="psb-badge">PSB</div>
            <div class="psb-title">Admin Access</div>
            <div class="psb-sub">Restricted — Authorized Personnel Only</div>
        </div>
    """, unsafe_allow_html=True)

    pwd = st.text_input("Password", type="password", label_visibility="collapsed",
                        placeholder="Enter admin password")
    col_a, col_b = st.columns([3, 1])
    with col_a:
        if st.button("Unlock Dashboard", use_container_width=True):
            if pwd == ADMIN_PASSWORD:
                st.session_state["is_admin"] = True
                st.session_state["logo_clicks"] = 0
                st.rerun()
            else:
                st.error("Wrong password.")
    with col_b:
        if st.button("← Back"):
            st.session_state["logo_clicks"] = 0
            st.rerun()


# ----------------------------------------------------------------------
# UI — ADMIN DASHBOARD (Endo) — UNCHANGED FROM ORIGINAL
# ----------------------------------------------------------------------

def admin_dashboard():
    st.markdown("""
        <div class="psb-hero" style="padding-bottom:1rem;">
            <div class="psb-badge">PSB</div>
            <div class="psb-title">Admin Dashboard</div>
            <div class="psb-sub">Auto Loan Curing · Control Panel</div>
        </div>
    """, unsafe_allow_html=True)

    top_a, top_b = st.columns([1, 1])
    with top_a:
        if st.button("📊 Switch to Auto Stat Admin"):
            st.session_state["admin_process"] = "autostat"
            st.rerun()
    with top_b:
        if st.button("🔒 Log out"):
            st.session_state["is_admin"] = False
            st.session_state["admin_process"] = None
            st.rerun()

    st.divider()

    # --- DataGrid upload -------------------------------------------------
    st.subheader("① DataGrid Upload")
    st.caption("Upload the latest DataGrid export to update the duplicate-check list.")
    dg_file = st.file_uploader("DataGrid .xlsx", type=["xlsx"])
    if dg_file is not None:
        try:
            dg_df = pd.read_excel(dg_file)
            account_col = None
            for candidate in ["Account No.", "Account No", "ACCOUNT NUMBER", "Account Number"]:
                if candidate in dg_df.columns:
                    account_col = candidate
                    break
            if account_col is None:
                st.error(
                    "Couldn't find an account number column in this file. "
                    f"Columns found: {list(dg_df.columns)}"
                )
            else:
                accounts = [normalize_account(a) for a in dg_df[account_col].tolist()]
                accounts = [a for a in accounts if a]
                replace_datagrid_accounts(accounts)
                st.success(f"✅ DataGrid updated — {len(accounts)} accounts loaded.")
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")

    datagrid_set = load_datagrid_set()
    st.caption(f"Current DataGrid: **{len(datagrid_set)}** accounts in system.")

    st.divider()

    # --- Submissions table -------------------------------------------------
    st.subheader("② Review Submissions")
    df = load_submissions()

    if df.empty:
        st.info("No submissions yet.")
        return

    df["status"] = df["account_number"].apply(
        lambda a: "DUPLICATE — already in system" if a in datagrid_set else "NEW — ready to scrape"
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        placement_filter = st.multiselect("Placement", PLACEMENTS, default=PLACEMENTS)
    with col2:
        status_filter = st.multiselect(
            "Status",
            ["NEW — ready to scrape", "DUPLICATE — already in system"],
            default=["NEW — ready to scrape", "DUPLICATE — already in system"],
        )
    with col3:
        hide_exported = st.checkbox("Hide exported rows", value=True)

    filtered = df[
        df["placement"].isin(placement_filter) & df["status"].isin(status_filter)
    ]
    if hide_exported:
        filtered = filtered[filtered["exported"] == 0]

    st.dataframe(
        filtered[
            [
                "id",
                "account_number",
                "endo_date",
                "agent",
                "placement",
                "status",
                "submitted_at",
                "exported",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        f"Showing {len(filtered)} of {len(df)} total. "
        f"{(df['status'] == 'NEW — ready to scrape').sum()} new / not duplicate."
    )

    st.divider()

    # --- Download ------------------------------------------------------
    st.subheader("③ Download for CAMS SCRAPE")
    st.caption(
        "Downloads only NEW, not-yet-exported rows formatted per placement. "
        "Rows are marked exported after download."
    )

    to_export = filtered[filtered["status"] == "NEW — ready to scrape"]

    if to_export.empty:
        st.info("Nothing new to export with the current filters.")
    else:
        workbook_bytes = build_export_workbook(to_export)
        clicked = st.download_button(
            label=f"⬇ Download {len(to_export)} new account(s) as Excel",
            data=workbook_bytes,
            file_name=f"NEW_ENDO_export_{date.today().isoformat()}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        if clicked:
            mark_exported(to_export["id"].tolist())
            st.success("Marked as exported. Refresh to see the updated list.")

    st.divider()

    # --- CAMS Reconciliation / Retry Export --------------------------------
    st.subheader("④ CAMS Retry — Didn't Go Through?")
    st.caption(
        "Upload the CAMS scrape output. The app finds which exported accounts "
        "are missing from it and builds a retry Excel in CAMS format."
    )

    cams_file = st.file_uploader("CAMS scrape output (.xlsx)", type=["xlsx"], key="cams_reconcile")
    if cams_file is not None:
        try:
            cams_df = pd.read_excel(cams_file)
            cams_acct_col = None
            for candidate in ["NEW ACCOUNT NUMBER", "ACCOUNT NUMBER", "Account Number", "Account No.", "Account No"]:
                if candidate in cams_df.columns:
                    cams_acct_col = candidate
                    break

            if cams_acct_col is None:
                st.error(
                    f"Couldn't find an account number column in this CAMS file. "
                    f"Columns found: {list(cams_df.columns)}"
                )
            else:
                cams_accounts_in_file = {
                    normalize_account(a)
                    for a in cams_df[cams_acct_col].tolist()
                    if normalize_account(a)
                }

                all_submissions = load_submissions()
                exported_submissions = all_submissions[all_submissions["exported"] == 1].copy()

                if exported_submissions.empty:
                    st.info("No exported submissions found to compare against.")
                else:
                    exported_submissions["in_cams"] = exported_submissions["account_number"].apply(
                        lambda a: a in cams_accounts_in_file
                    )
                    missed = exported_submissions[~exported_submissions["in_cams"]].copy()

                    col_a, col_b = st.columns(2)
                    col_a.metric("Exported accounts", len(exported_submissions))
                    col_b.metric("❌ Missed in CAMS", len(missed))

                    if missed.empty:
                        st.success("✅ All exported accounts found in CAMS — nothing missed!")
                    else:
                        st.warning(f"{len(missed)} account(s) not found in CAMS output.")
                        st.dataframe(
                            missed[["id", "account_number", "endo_date", "agent", "placement", "exported_at"]],
                            use_container_width=True,
                            hide_index=True,
                        )
                        retry_bytes = build_export_workbook(missed)
                        st.download_button(
                            label=f"⬇ Download {len(missed)} missed account(s) — CAMS Retry Excel",
                            data=retry_bytes,
                            file_name=f"CAMS_RETRY_{date.today().isoformat()}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
        except Exception as e:
            st.error(f"Couldn't read that file: {e}")

    st.divider()
    bulk_paste_endo_section()


# ========================================================================
# NEW: UI — ADMIN DASHBOARD (Auto Stat)
# ========================================================================

def autostat_admin_dashboard():
    st.markdown("""
        <div class="psb-hero" style="padding-bottom:1rem;">
            <div class="psb-badge">PSB</div>
            <div class="psb-title">Auto Stat — Admin Dashboard</div>
            <div class="psb-sub">Auto Loan Curing · Status Update Control Panel</div>
        </div>
    """, unsafe_allow_html=True)

    top_a, top_b = st.columns([1, 1])
    with top_a:
        if st.button("← Switch to Endo Admin"):
            st.session_state["admin_process"] = "endo"
            st.rerun()
    with top_b:
        if st.button("🔒 Log out"):
            st.session_state["is_admin"] = False
            st.session_state["logo_clicks"] = 0
            st.session_state["admin_process"] = None
            st.rerun()

    st.divider()

    st.subheader("① Review Submissions")
    df = load_autostat_submissions()

    if df.empty:
        st.info("No Auto Stat submissions yet.")
        return

    df["has_claim"] = df.apply(
        lambda r: "CLAIM PAID" if (str(r.get("claim_paid_date", "")).strip()
                                    or _parse_amount(r.get("claim_paid_amount", "")) not in (None, 0))
        else "NO CLAIM",
        axis=1,
    )

    col1, col2 = st.columns(2)
    with col1:
        claim_filter = st.multiselect(
            "Claim Status", ["NO CLAIM", "CLAIM PAID"], default=["NO CLAIM", "CLAIM PAID"]
        )
    with col2:
        hide_exported = st.checkbox("Hide exported rows", value=True)

    filtered = df[df["has_claim"].isin(claim_filter)]
    if hide_exported:
        filtered = filtered[filtered["exported"] == 0]

    st.dataframe(
        filtered[
            [
                "id",
                "account_number",
                "status_code",
                "remarks",
                "ptp_date",
                "ptp_amount",
                "claim_paid_date",
                "claim_paid_amount",
                "collector",
                "submitted_at",
                "has_claim",
                "exported",
            ]
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.caption(f"Showing {len(filtered)} of {len(df)} total submissions.")

    st.divider()

    st.subheader("② Download — Clean + Claim Paid (separate sheets)")
    st.caption(
        "Duplicate account numbers get their REMARKS DATE bumped +1 minute "
        "automatically. Rows with a Claim Paid Date/Amount are split into "
        "their own 'CLAIM PAID' sheet and excluded from 'CLEAN'. "
        "Rows are marked exported after download."
    )

    # Export always includes ALL rows (both CLEAN and CLAIM PAID), regardless
    # of the display filter above. Only the hide_exported toggle affects this.
    to_export = df[df["exported"] == 0] if hide_exported else df

    if to_export.empty:
        st.info("Nothing new to export with the current filters.")
    else:
        workbook_bytes, clean_count, claim_count, _ = build_autostat_export_workbook(to_export)
        st.caption(f"Preview: **{clean_count}** row(s) → CLEAN sheet · **{claim_count}** row(s) → CLAIM PAID sheet")

        dl_col1, dl_col2 = st.columns(2)
        with dl_col1:
            clicked = st.download_button(
                label=f"⬇ Full Export — Both Sheets ({len(to_export)} rows)",
                data=workbook_bytes,
                file_name=f"AUTOSTAT_export_{date.today().isoformat()}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_full_export",
            )
            if clicked:
                mark_autostat_exported(to_export["id"].tolist())
                st.success("Marked as exported. Refresh to see the updated list.")

        with dl_col2:
            claim_only_bytes, claim_only_count = build_claim_paid_only_workbook(to_export)
            if claim_only_count > 0:
                st.download_button(
                    label=f"💳 Claim Paid Only ({claim_only_count} rows)",
                    data=claim_only_bytes,
                    file_name=f"AUTOSTAT_CLAIM_PAID_{date.today().isoformat()}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="dl_claim_only",
                )
            else:
                st.info("No CLAIM PAID rows in current data.")

    st.divider()
    excel_upload_autostat_section()

    st.divider()
    bulk_paste_autostat_section()


# ========================================================================
# NEW: UI — ADMIN PROCESS PICKER
# ========================================================================

def admin_picker_page():
    st.markdown("""
        <div class="psb-hero" style="padding-bottom:1rem;">
            <div class="psb-badge">PSB</div>
            <div class="psb-title">Admin — Choose a Process</div>
            <div class="psb-sub">Auto Loan Curing</div>
        </div>
    """, unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown('<div class="process-pick-btn">', unsafe_allow_html=True)
        if st.button("🧾 Endo Admin", use_container_width=True):
            st.session_state["admin_process"] = "endo"
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)
    with c2:
        st.markdown('<div class="process-pick-btn">', unsafe_allow_html=True)
        if st.button("📊 Auto Stat Admin", use_container_width=True):
            st.session_state["admin_process"] = "autostat"
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    st.divider()
    if st.button("🔒 Log out"):
        st.session_state["is_admin"] = False
        st.session_state["logo_clicks"] = 0
        st.rerun()


def admin_router():
    """NEW: sits in front of the two admin dashboards, doesn't change
    either one's internal code."""
    admin_process = st.session_state.get("admin_process")
    if admin_process == "endo":
        admin_dashboard()  # original, untouched
    elif admin_process == "autostat":
        autostat_admin_dashboard()
    else:
        admin_picker_page()


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="PSB New Endo Process",
        page_icon="🔴",
        layout="wide",
    )

    inject_styles()

    if "db_initialized" not in st.session_state:
        init_db()
        st.session_state["db_initialized"] = True

    if "is_admin" not in st.session_state:
        st.session_state["is_admin"] = False
    if "agent_process" not in st.session_state:  # NEW
        st.session_state["agent_process"] = None
    if "admin_process" not in st.session_state:  # NEW
        st.session_state["admin_process"] = None

    # ── Secret admin trigger via URL query param ────────────────────────
    # Agents use the normal URL → agent form.
    # You access admin by adding ?admin=1 to the URL, e.g.:
    #   http://localhost:8501?admin=1
    # No button, no visible link — agents will never see it.
    params = st.query_params
    if params.get("admin") == "1" and not st.session_state["is_admin"]:
        admin_login()
    elif st.session_state["is_admin"]:
        admin_router()  # CHANGED: was admin_dashboard() directly
    else:
        # NEW: agent picks Endo or Auto Stat first
        agent_process = st.session_state["agent_process"]
        if agent_process == "endo":
            if st.button("← Back to process picker"):
                st.session_state["agent_process"] = None
                st.rerun()
            agent_form_page()  # original, untouched
        elif agent_process == "autostat":
            if st.button("← Back to process picker"):
                st.session_state["agent_process"] = None
                st.rerun()
            autostat_form_page()
        else:
            process_picker_page()


if __name__ == "__main__":
    main()