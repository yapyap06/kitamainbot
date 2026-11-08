import os
import io
import re
import json
import urllib.parse
import requests
from datetime import datetime
from fastapi import FastAPI, Request, Form
from fastapi.responses import Response, HTMLResponse, JSONResponse
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

OCR_ENGINE = None

# 1. Try tesserocr (C-binding, fast, no external exe required)
try:
    import tesserocr
    from PIL import Image
    # Check if tessdata exists locally or in system
    _local_tessdata = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tessdata")
    _tess_path = _local_tessdata if os.path.exists(_local_tessdata) else None
    OCR_ENGINE = "tesserocr"
    print(f"[OCR Init] tesserocr loaded successfully (tessdata: {_tess_path or 'system default'})")
except Exception as _e1:
    # 2. Try pytesseract (requires tesseract.exe installed)
    try:
        import pytesseract
        from PIL import Image
        for _p in [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe")
        ]:
            if os.path.exists(_p):
                pytesseract.pytesseract.tesseract_cmd = _p
                break
        pytesseract.get_tesseract_version()
        OCR_ENGINE = "pytesseract"
        print("[OCR Init] pytesseract loaded successfully")
    except Exception as _e2:
        # 3. Fallback to easyocr if already installed
        try:
            import easyocr
            EASYOCR_READER = easyocr.Reader(["ms", "en"], gpu=False)
            OCR_ENGINE = "easyocr"
            print("[OCR Init] easyocr loaded as fallback")
        except Exception as _e3:
            OCR_ENGINE = None
            print(f"[OCR Init Error] No OCR engine available: {_e1} | {_e2} | {_e3}")

TESSERACT_AVAILABLE = (OCR_ENGINE is not None)

try:
    from pypdf import PdfReader
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    import gspread
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

app = FastAPI(title="Kita Main (YGG) WhatsApp Intake & 3-Sheet Live Engine")

@app.get("/")
def home():
    return {"status": "online", "message": "Kita Main WhatsApp Bot is live!"}

@app.get("/health")
def health():
    return {"status": "ok"}

# =====================================================================
# 1. TWILIO & GOOGLE SHEETS CREDENTIALS (from environment variables)
# =====================================================================
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

# Support credentials.json as a file OR as a JSON string in env var
_creds_json_str = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
if _creds_json_str:
    _creds_path = "/tmp/gcp_credentials.json"
    with open(_creds_path, "w") as _f:
        _f.write(_creds_json_str)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _creds_path
elif os.path.exists("credentials.json"):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "credentials.json"

GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "KitaMain_Master_DB")

# 3 Worksheet Tab Names
TAB_INTAKE = "1_Current_Intake_Window"
TAB_REVIEW = "2_Under_Review_Officer"
TAB_WAITLIST = "3_Eligible_Waitlist"

# Batch Capacity Settings (800 slots for Current Intake Window)
BATCH_CAPACITY = 800
APPROVED_ACTIVE_COUNT = 0
WAITLIST_COUNTER = 0

SESSIONS = {}
MASTER_DB = {}

# Sets for Duplicate & Fraud Detection
REGISTERED_ICS = set()
REGISTERED_MYKIDS = set()
REGISTERED_ADDRESSES = set()

# Referral System Lookup: maps referral_code (e.g. "KM-5522") -> referrer's sender_key
REFERRAL_CODE_TO_SENDER = {}

SHEET_HEADERS = [
    "A: Timestamp & Session",
    "B: Recipient_Name (Shipping)",
    "C: Recipient_Phone (WhatsApp)",
    "D: Delivery_Address (Shipping)",
    "E: Parent_IC",
    "F: Child_DOB_MyKid",
    "G: Child_Age_Bucket & Kit",
    "H: Proof_Type",
    "I: Evidence_URL",
    "J: OCR_Verification",
    "K: Flagged_Events_JSON",
    "L: PDPA_Consent",
    "M: Baseline & Referral_Info",
    "N: Application_Status",
    "O: Slot_Or_Waitlist_Rank",
    "P: Officer_Decision (PASS/FAIL)",
    "Q: Officer_Custom_Message",
    "R: WhatsApp_Click_To_Chat_Link"
]


# =====================================================================
# 2. HELPER FUNCTIONS: NORMALIZATION, REFERRAL & WA.ME LINK GENERATOR
# =====================================================================
def normalize_address(addr: str) -> str:
    """Normalizes address string to detect duplicate household address entries."""
    if not addr:
        return ""
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", addr.lower())
    return cleaned


def generate_referral_code(name: str, ic: str, phone: str) -> str:
    """Generates a unique, easy-to-type referral code (e.g., KM-SA5522)."""
    alpha = re.sub(r"[^A-Z]", "", (name or "KM").upper())
    prefix = alpha[:2] if len(alpha) >= 2 else "KM"
    digits = re.sub(r"\D", "", ic or "")
    suffix = digits[-4:] if len(digits) >= 4 else re.sub(r"\D", "", phone or "")[-4:]
    base_code = f"KM-{prefix}{suffix}"
    # Ensure uniqueness if two people share the same prefix + last 4 digits
    counter = 1
    final_code = base_code
    while final_code in REFERRAL_CODE_TO_SENDER:
        final_code = f"{base_code}{counter}"
        counter += 1
    return final_code


def format_col_m_baseline_and_referral(
    baseline: str,
    own_code: str,
    used_code: str,
    ref_count: int
) -> str:
    """Formats Column M with Baseline Survey, Own Referral Code, Used Code, and Extra Toy Reward status."""
    used_display = used_code if used_code else "Tiada"
    if ref_count > 0:
        reward_str = f"🎁 LAYAK MAINAN TAMBAHAN ({ref_count} Rujukan)"
    else:
        reward_str = "Tiada Rujukan (0)"
    return f"{baseline} | Kod: {own_code} | Guna: {used_display} | {reward_str}"


def build_wa_me_url(phone_raw: str, message_text: str) -> str:
    """Builds a 100% free WhatsApp Click-to-Chat URL (https://wa.me/...)."""
    clean_phone = re.sub(r"\D", "", phone_raw or "")
    if clean_phone.startswith("0"):
        clean_phone = "6" + clean_phone
    encoded_msg = urllib.parse.quote(message_text or "")
    return f"https://wa.me/{clean_phone}?text={encoded_msg}"


def build_wa_hyperlink_formula(phone_raw: str, message_text: str, label: str = "💬 Klik Chat Pelanggan") -> str:
    """Creates a clickable Google Sheets =HYPERLINK() formula for Column R."""
    url = build_wa_me_url(phone_raw, message_text)
    safe_url = url.replace('"', '""')
    safe_label = label.replace('"', '""')
    return f'=HYPERLINK("{safe_url}", "{safe_label}")'


# =====================================================================
# 3. 3-SHEET GOOGLE SHEETS ENGINE (AUTO-TABS, SYNC & MOVE ON PASS)
# =====================================================================
def get_spreadsheet():
    """Connects to Google Sheets and ensures all 3 worksheets exist with headers."""
    global APPROVED_ACTIVE_COUNT, WAITLIST_COUNTER
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
    if not GSPREAD_AVAILABLE or not os.path.exists(creds_path):
        print(f"[Sheets Warning] Credentials file not found ({creds_path}) or gspread not installed.")
        return None
    try:
        gc = gspread.service_account(filename=creds_path)
        sh = gc.open(GOOGLE_SHEET_NAME)
        existing_titles = [w.title for w in sh.worksheets()]

        # Rename default 'Sheet1' to '1_Current_Intake_Window' if needed
        if "Sheet1" in existing_titles and TAB_INTAKE not in existing_titles:
            sh.worksheet("Sheet1").update_title(TAB_INTAKE)
            existing_titles = [w.title for w in sh.worksheets()]

        for tab_name in [TAB_INTAKE, TAB_REVIEW, TAB_WAITLIST]:
            if tab_name not in existing_titles:
                ws = sh.add_worksheet(title=tab_name, rows=1000, cols=20)
            else:
                ws = sh.worksheet(tab_name)

            headers = ws.row_values(1)
            if not headers or len(headers) < len(SHEET_HEADERS):
                ws.update("A1:R1", [SHEET_HEADERS])
                ws.format("A1:R1", {
                    "backgroundColor": {"red": 0.12, "green": 0.16, "blue": 0.23},
                    "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}
                })

        return sh
    except Exception as e:
        print(f"[Google Sheets Connection Error]: {e}")
        return None


def refresh_counters_and_duplicates_from_sheets():
    """Loads existing ICs, MyKids, Addresses, Referral Codes, and Slot/Waitlist counts from the 3 sheets."""
    global APPROVED_ACTIVE_COUNT, WAITLIST_COUNTER
    sh = get_spreadsheet()
    if not sh:
        return
    try:
        ws_intake = sh.worksheet(TAB_INTAKE)
        ws_review = sh.worksheet(TAB_REVIEW)
        ws_waitlist = sh.worksheet(TAB_WAITLIST)

        intake_rows = ws_intake.get_all_values()[1:]
        review_rows = ws_review.get_all_values()[1:]
        waitlist_rows = ws_waitlist.get_all_values()[1:]

        APPROVED_ACTIVE_COUNT = len([r for r in intake_rows if any(r)])
        WAITLIST_COUNTER = len([r for r in waitlist_rows if any(r)])

        for row in (intake_rows + review_rows + waitlist_rows):
            if len(row) >= 6:
                addr_norm = normalize_address(row[3])
                ic_val = re.sub(r"\D", "", row[4])
                mykid_match = re.match(r"^(\d{12})", row[5].lstrip("'").strip())
                if addr_norm:
                    REGISTERED_ADDRESSES.add(addr_norm)
                if len(ic_val) == 12:
                    REGISTERED_ICS.add(ic_val)
                if mykid_match:
                    REGISTERED_MYKIDS.add(mykid_match.group(1))
            # Reload existing referral codes from Column M if server restarted
            if len(row) >= 13:
                phone_val = row[2].strip()
                code_match = re.search(r"Kod:\s*(KM-[A-Z0-9]+)", row[12])
                if phone_val and code_match:
                    REFERRAL_CODE_TO_SENDER[code_match.group(1)] = f"whatsapp:{phone_val}"
    except Exception as e:
        print(f"[Sheets Counter Sync Warning]: {e}")


@app.on_event("startup")
def on_startup():
    refresh_counters_and_duplicates_from_sheets()


def record_to_row_values(record: dict) -> list:
    """Converts a record dictionary into an 18-column row list (A to R), preserving leading zeros in E & F."""
    ic_str = str(record.get("col_E_parent_ic", ""))
    mykid_str = str(record.get("col_F_child_mykid", ""))
    return [
        record.get("col_A_timestamp_phone", ""),
        record.get("col_B_recipient_name", ""),
        record.get("col_C_recipient_phone", ""),
        record.get("col_D_address", ""),
        f"'{ic_str}" if ic_str and not ic_str.startswith("'") else ic_str,
        f"'{mykid_str}" if mykid_str and not mykid_str.startswith("'") else mykid_str,
        record.get("col_G_age_bucket", ""),
        record.get("col_H_proof_type", ""),
        record.get("col_I_evidence_url", ""),
        record.get("col_J_ocr_verdict", ""),
        record.get("col_K_flagged_json", "{}"),
        record.get("col_L_pdpa_consent", ""),
        record.get("col_M_baseline", ""),
        record.get("col_N_status", ""),
        record.get("col_O_rank", ""),
        record.get("col_P_officer_decision", ""),
        record.get("col_Q_custom_msg", ""),
        record.get("col_R_wa_link", "")
    ]


def delete_phone_from_worksheet(ws, phone_clean: str):
    """Deletes an applicant's row from a worksheet (used when moving between tabs)."""
    if not ws or not phone_clean:
        return
    try:
        col_a = ws.col_values(1)
        for idx in range(len(col_a), 1, -1):
            if phone_clean in col_a[idx - 1]:
                ws.delete_rows(idx)
    except Exception as e:
        print(f"[Worksheet Row Delete Error]: {e}")


def sync_to_google_sheet(record: dict, target_tab: str = None, remove_from_tabs: list = None):
    """Appends or updates the applicant's row in the appropriate worksheet tab (1, 2, or 3)."""
    sh = get_spreadsheet()
    if not sh:
        return
    try:
        status_str = record.get("col_N_status", "")
        if not target_tab:
            if "FLAGGED" in status_str or "REUPLOAD" in status_str or "REJECTED" in status_str:
                target_tab = TAB_REVIEW
            elif "WAITLIST" in status_str:
                target_tab = TAB_WAITLIST
            else:
                target_tab = TAB_INTAKE

        phone_clean = record.get("phone_raw", "")

        # Remove from previous tabs if moving (e.g., from Sheet 2 Review -> Sheet 1 Intake)
        if remove_from_tabs:
            for old_tab in remove_from_tabs:
                if old_tab != target_tab:
                    delete_phone_from_worksheet(sh.worksheet(old_tab), phone_clean)

        ws = sh.worksheet(target_tab)
        row_values = record_to_row_values(record)

        col_a_values = ws.col_values(1)
        target_row = None
        for idx, val in enumerate(col_a_values, start=1):
            if idx > 1 and phone_clean and phone_clean in val:
                target_row = idx
                break

        if target_row:
            ws.update(f"A{target_row}:R{target_row}", [row_values], value_input_option="USER_ENTERED")
        else:
            ws.append_row(row_values, value_input_option="USER_ENTERED")
            target_row = len(col_a_values) + 1

        # Color-code row based on status
        if "FLAGGED" in status_str or "REUPLOAD" in status_str or "REJECTED" in status_str:
            bg_color = {"red": 1.0, "green": 0.90, "blue": 0.90}  # Light Red
        elif "WAITLIST" in status_str:
            bg_color = {"red": 1.0, "green": 0.97, "blue": 0.85}  # Soft Yellow/Amber
        elif "EXPIRED" in status_str:
            bg_color = {"red": 0.93, "green": 0.93, "blue": 0.93}  # Grey
        else:
            bg_color = {"red": 0.90, "green": 0.98, "blue": 0.92}  # Light Green

        ws.format(f"A{target_row}:R{target_row}", {"backgroundColor": bg_color})
        print(f"✅ [Google Sheets Synced] [{target_tab}] Row {target_row} ({record.get('col_B_recipient_name')}) -> {status_str}")
    except Exception as e:
        print(f"[Google Sheets Write Error]: {e}")


def reward_referrer_in_sheet_and_memory(ref_code: str, new_applicant_name: str) -> bool:
    """
    Credits the referrer when a friend registers using their referral code.
    Updates both in-memory MASTER_DB and Google Sheets Column M, and sends a WhatsApp notification.
    """
    ref_code_clean = ref_code.strip().upper()
    referrer_sender = REFERRAL_CODE_TO_SENDER.get(ref_code_clean)

    # 1. If referrer is in active memory (MASTER_DB)
    if referrer_sender and referrer_sender in MASTER_DB:
        ref_record = MASTER_DB[referrer_sender]
        ref_user = SESSIONS.get(referrer_sender, {})
        new_count = ref_user.get("referral_count", 0) + 1
        ref_user["referral_count"] = new_count

        ref_record["col_M_baseline"] = format_col_m_baseline_and_referral(
            baseline=ref_user.get("baseline", "Rekod Sedia Ada"),
            own_code=ref_user.get("referral_code", ref_code_clean),
            used_code=ref_user.get("used_referral_code", ""),
            ref_count=new_count
        )
        sync_to_google_sheet(ref_record)

        congrats_msg = (
            f"🎁 *Tahniah {ref_record.get('col_B_recipient_name', 'Ibu/Bapa')}!* "
            f"Rakan anda (*{new_applicant_name}*) baru sahaja mendaftar menggunakan Kod Rujukan anda (*{ref_code_clean}*).\n\n"
            f"🧸 Anda kini layak menerima *1 Mainan Tambahan PERCUMA* di dalam Kotak Kita Main anda yang seterusnya! "
            f"(Jumlah rujukan berjaya: *{new_count}*)"
        )
        send_outbound_whatsapp(referrer_sender, congrats_msg)
        return True

    # 2. Fallback: Search across the 3 Google Sheet tabs if server was restarted
    sh = get_spreadsheet()
    if not sh:
        return False
    try:
        for tab_name in [TAB_INTAKE, TAB_REVIEW, TAB_WAITLIST]:
            ws = sh.worksheet(tab_name)
            all_rows = ws.get_all_values()
            for row_idx, row in enumerate(all_rows, start=1):
                if row_idx == 1 or len(row) < 13:
                    continue
                col_m = row[12]
                if f"Kod: {ref_code_clean}" in col_m:
                    cnt_match = re.search(r"\((\d+)\s*Rujukan\)", col_m)
                    new_cnt = (int(cnt_match.group(1)) + 1) if cnt_match else 1
                    parts = [p.strip() for p in col_m.split("|")]
                    if len(parts) >= 3:
                        updated_col_m = f"{parts[0]} | {parts[1]} | {parts[2]} | 🎁 LAYAK MAINAN TAMBAHAN ({new_cnt} Rujukan)"
                    else:
                        updated_col_m = f"{col_m} | 🎁 LAYAK MAINAN TAMBAHAN ({new_cnt} Rujukan)"
                    ws.update_cell(row_idx, 13, updated_col_m)
                    ref_phone = row[2].strip()
                    ref_name = row[1].strip()
                    if ref_phone:
                        send_outbound_whatsapp(
                            f"whatsapp:{ref_phone}",
                            f"🎁 *Tahniah {ref_name}!* Rakan anda (*{new_applicant_name}*) telah mendaftar menggunakan kod *{ref_code_clean}*. "
                            f"Anda layak mendapat *Mainan Tambahan PERCUMA* dalam kotak seterusnya! (Jumlah rujukan: *{new_cnt}*)"
                        )
                    return True
    except Exception as e:
        print(f"[Referral Sheet Update Warning]: {e}")
    return False


# =====================================================================
# 4. TWILIO OUTBOUND MESSAGING & DUAL-CHECK OCR / PDF ENGINE
# =====================================================================
def send_outbound_whatsapp(to_number: str, message_text: str) -> bool:
    """Attempts to send an active WhatsApp message via Twilio REST API (falls back gracefully if trial blocked)."""
    try:
        client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        from_num = TWILIO_WHATSAPP_NUMBER if TWILIO_WHATSAPP_NUMBER.startswith("whatsapp:") else f"whatsapp:{TWILIO_WHATSAPP_NUMBER}"
        to_num = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"
        client.messages.create(from_=from_num, to=to_num, body=message_text)
        return True
    except Exception as e:
        print(f"[Twilio Outbound Notice - Use Sheet Col R wa.me Link]: {e}")
        return False


def download_twilio_media(media_url: str) -> bytes:
    """Downloads image or PDF from Twilio Media URL using Account SID & Auth Token."""
    resp = requests.get(media_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15)
    if resp.status_code == 200:
        return resp.content
    return requests.get(media_url, timeout=15).content


def validate_mykid_or_dob(raw_input: str) -> tuple[bool, str, str, str]:
    """
    Extracts YYMMDD from 12-digit MyKid or 8-digit DOB (DDMMYYYY), checks age <= 36m (born 2023-2026).
    Returns: (is_valid, kit_tier_or_error, formatted_string, clean_12digit_mykid_or_empty)
    """
    digits = re.sub(r"\D", "", raw_input)
    now = datetime(2026, 11, 6)
    try:
        clean_mykid = ""
        if len(digits) == 12:
            clean_mykid = digits
            yy, mm, dd = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
            year = 2000 + yy if yy <= 30 else 1900 + yy
            dob = datetime(year, mm, dd)
        elif len(digits) == 8:
            dd, mm, year = int(digits[0:2]), int(digits[2:4]), int(digits[4:8])
            dob = datetime(year, mm, dd)
        else:
            return False, "INVALID_FORMAT", "", ""

        age_months = (now.year - dob.year) * 12 + (now.month - dob.month)
        if dob.year < 2023 or age_months > 36:
            return False, "OVER_AGE", f"{age_months} bulan (Lahir {dob.strftime('%d/%m/%Y')})", clean_mykid
        if age_months < 0:
            return False, "INVALID_DATE", "", clean_mykid

        if age_months <= 12:
            kit_tier = "Box 1 (0–12 Bulan)"
        elif age_months <= 24:
            kit_tier = "Box 2 (13–24 Bulan)"
        else:
            kit_tier = "Box 3 (25–36 Bulan)"

        return True, kit_tier, f"{digits} ({age_months} bulan - {kit_tier})", clean_mykid
    except ValueError:
        return False, "INVALID_DATE", "", ""


def verify_evidence_document(
    media_url: str,
    media_type: str,
    proof_type: str,
    expected_ic: str,
    expected_name: str
) -> tuple[str, str]:
    """Runs OCR via Tesseract (images) or PyPDF (PDFs) to verify eligibility documents."""
    try:
        content = download_twilio_media(media_url)
        extracted_text = ""

        # 1. Extract text from PDF (e.g., Surat Tawaran PPR)
        if "pdf" in media_type.lower() or media_url.lower().endswith(".pdf"):
            if PDF_AVAILABLE:
                reader = PdfReader(io.BytesIO(content))
                for page in reader.pages:
                    extracted_text += (page.extract_text() or "") + "\n"
            else:
                return "FLAGGED_REVIEW", "MANUAL_PDF_DOC (Semakan pegawai)"
        # 2. Extract text from Image (STR / MySalam Screenshot) using available OCR Engine
        else:
            if OCR_ENGINE == "tesserocr":
                image = Image.open(io.BytesIO(content))
                tess_args = {"lang": "msa+eng"}
                if _tess_path:
                    tess_args["path"] = _tess_path
                with tesserocr.PyTessBaseAPI(**tess_args) as api:
                    api.SetImage(image)
                    extracted_text = api.GetUTF8Text()
            elif OCR_ENGINE == "pytesseract":
                image = Image.open(io.BytesIO(content))
                extracted_text = pytesseract.image_to_string(image, lang="msa+eng")
            elif OCR_ENGINE == "easyocr":
                results = EASYOCR_READER.readtext(content, detail=0)
                extracted_text = " ".join(results)
            else:
                return "FLAGGED_REVIEW", "OCR_OFFLINE (Tesseract/EasyOCR tidak tersedia - Semakan Pegawai)"

        if not extracted_text.strip():
            return "FLAGGED_REVIEW", "OCR_UNREADABLE (Tiada teks ditemui / imej kabur)"

        full_text = extracted_text.upper()
        clean_digits = re.sub(r"\D", "", full_text)

        has_str_lulus = any(
            w in full_text for w in ["LULUS", "LAYAK", "BERJAYA", "DITERIMA", "DILULUSKAN"]
        )
        has_mysalam_b40 = ("MYSALAM" in full_text and "B40" in full_text)
        has_ppr_keyword = ("PPR" in full_text or "PROGRAM PERUMAHAN RAKYAT" in full_text)

        ic_match = (expected_ic[-4:] in clean_digits) if len(expected_ic) >= 4 else False
        name_tokens = [
            t for t in re.split(r"\s+", expected_name.upper())
            if len(t) > 3 and t not in {"BINTI", "BIN", "A/P", "A/L"}
        ]
        name_match = any(token in full_text for token in name_tokens) if name_tokens else False
        identity_matched = ic_match or name_match

        if proof_type == "Bil/Surat PPR":
            if (has_ppr_keyword or has_str_lulus) and identity_matched:
                return "FLAGGED_REVIEW", "MANUAL_PPR_DOC (Teks PPR & Nama/IC Padan - Sedia Diluluskan Pegawai)"
            return "FLAGGED_REVIEW", "MANUAL_PPR_DOC (Dokumen PPR memerlukan pengesahan pegawai)"

        if (has_str_lulus or has_mysalam_b40) and identity_matched:
            matched_tag = "MySalam B40" if has_mysalam_b40 and not has_str_lulus else "Status LULUS/LAYAK"
            return "VERIFIED", f"Auto-OCR Lulus ({matched_tag} & Identiti Padan)"
        elif (has_str_lulus or has_mysalam_b40) and not identity_matched:
            return "FLAGGED_REVIEW", "IC_MISMATCH (Status LULUS/B40 ditemui tetapi Nama/No. IC tidak sepadan)"
        else:
            return "FLAGGED_REVIEW", "OCR_UNREADABLE (Perkataan 'LULUS'/'LAYAK' tidak jelas)"

    except Exception as e:
        return "FLAGGED_REVIEW", f"OCR_ERROR ({str(e)})"


# =====================================================================
# 5. WHATSAPP CONVERSATIONAL FLOW WEBHOOK (/webhook)
# =====================================================================
@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    global APPROVED_ACTIVE_COUNT, WAITLIST_COUNTER, BATCH_CAPACITY
    form_data = await request.form()
    raw_from = form_data.get("From", "unknown").replace("whatsapp:", "").strip()
    wa_id = form_data.get("WaId", "").strip()
    body = form_data.get("Body", "").strip()
    num_media = int(form_data.get("NumMedia", 0))

    # Convert WhatsApp's masked BSUID (MY.xxxx) to your real phone number
    BSUID_PHONE_MAP = {
        "MY.1805029107119294": "+60109469868",  # <-- Tukar kepada nombor WhatsApp sebenar anda
    }

    if raw_from in BSUID_PHONE_MAP:
        clean_phone = BSUID_PHONE_MAP[raw_from]
    elif wa_id and wa_id.isdigit():
        clean_phone = f"+{wa_id}"
    else:
        clean_phone = raw_from

    sender = f"whatsapp:{clean_phone}"

    resp = MessagingResponse()

    # Demo Helper Commands
    if body.upper() == "RESET":
        old_user = SESSIONS.get(sender, {})
        if old_user.get("ic"):
            REGISTERED_ICS.discard(old_user["ic"])
        if old_user.get("clean_mykid"):
            REGISTERED_MYKIDS.discard(old_user["clean_mykid"])
        if old_user.get("norm_address"):
            REGISTERED_ADDRESSES.discard(old_user["norm_address"])
        if old_user.get("referral_code"):
            REFERRAL_CODE_TO_SENDER.pop(old_user["referral_code"], None)
        SESSIONS.pop(sender, None)
        MASTER_DB.pop(sender, None)
        resp.message("🔄 *Sesi Demo Ditetapkan Semula.*\nTaip *DAFTAR_KITAMAIN* atau *HI* untuk memulakan pendaftaran baharu.")
        return Response(content=str(resp), media_type="application/xml")

    if body.upper() == "DEMO_FULL":
        APPROVED_ACTIVE_COUNT = BATCH_CAPACITY
        resp.message(f"⚙️ *[Mod Demo]* Kuota Semasa ditetapkan kepada PENUH ({APPROVED_ACTIVE_COUNT}/{BATCH_CAPACITY}). Pendaftaran lulus seterusnya akan masuk ke *Sheet 3: Eligible - Waitlist*.")
        return Response(content=str(resp), media_type="application/xml")

    if body.upper() == "DEMO_OPEN":
        APPROVED_ACTIVE_COUNT = 0
        resp.message(f"⚙️ *[Mod Demo]* Kuota Semasa dibuka semula ({APPROVED_ACTIVE_COUNT}/{BATCH_CAPACITY}). Pendaftaran lulus seterusnya akan masuk ke *Sheet 1: Current Intake Window*.")
        return Response(content=str(resp), media_type="application/xml")

    if body.upper() == "OPEN_SLOT" and sender in SESSIONS:
        user = SESSIONS[sender]
        user["step"] = "WAITLIST_OFFER_DECISION"
        resp.message(
            f"🎁 *Berita Baik {user.get('name', 'Ibu/Bapa')}! Satu slot Kotak Kita Main kini dibuka untuk anak anda!*\n\n"
            f"• *Nama Penerima:* {user.get('name', 'Ibu/Bapa')}\n"
            f"• *No. Telefon:* {user.get('phone', '')}\n"
            f"• *Alamat:* {user.get('address', 'Alamat Anda')}\n\n"
            "Sila sahkan alamat penghantaran anda dalam masa *48 JAM* sebelum slot ini diberikan kepada keluarga seterusnya:\n"
            "👉 Balas *[1]* ✅ Sahkan Alamat\n"
            "👉 Balas *[2]* ✏️ Tukar Alamat\n"
            "👉 Balas *[3]* ❌ Batal\n"
            "_(Untuk demo: Taip *EXPIRE_48H* untuk simulasi auto-skip 48 jam)_"
        )
        return Response(content=str(resp), media_type="application/xml")

    if sender not in SESSIONS:
        SESSIONS[sender] = {
            "step": "CONSENT",
            "phone": sender.replace("whatsapp:", "").strip(),
            "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ic_attempts": 0,
            "duplicate_ic_flag": False,
            "duplicate_mykid_flag": False,
            "duplicate_address_flag": False,
            "input_error_flag": False,
            "referral_code": "",
            "used_referral_code": "",
            "referral_count": 0
        }
        resp.message(
            "💛 *Selamat datang ke Program Kita Main!*\n"
            "Terima kasih atas minat anda terhadap Program Kita Main oleh Yayasan Generasi Gemilang.\n\n"
            "📚 *Apa itu Program Kita Main?*\n"
            "Program ini menyokong perkembangan awal kanak-kanak berumur 0–3 tahun. Keluarga yang layak akan menerima *3 Kotak Kita Main secara PERCUMA* sepanjang tempoh kira-kira 8 bulan, lengkap dengan buku, permainan dan aktiviti untuk ibu bapa dan anak.\n\n"
            "📋 *Sebelum meneruskan, sila pastikan anda memenuhi syarat berikut:*\n"
            "• Warganegara Malaysia yang menetap di Semenanjung Malaysia.\n"
            "• Mempunyai anak berumur 3 tahun ke bawah (lahir 2023–2026).\n"
            "• Keluarga B40, penerima STR/e-Kasih atau menetap di komuniti PPR.\n"
            "• Belum pernah menerima set Program Kita Main (1 set bagi setiap isi rumah).\n\n"
            "Pendaftaran hanya mengambil masa 2 minit. Sebelum bermula, adakah anda bersetuju maklumat anda disimpan mengikut Akta Perlindungan Data Peribadi (PDPA) untuk tujuan semakan kelayakan & penghantaran kotak?\n\n"
            "👉 Balas *[1]* ✅ Saya Setuju\n"
            "👉 Balas *[2]* ❌ Tidak Setuju"
        )
        return Response(content=str(resp), media_type="application/xml")

    user = SESSIONS[sender]
    step = user["step"]

    if step == "CONSENT":
        if body in ["1", "SAYA SETUJU", "SETUJU"]:
            user["consent_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            user["step"] = "Q1_AGE_TRIAGE"
            resp.message(
                "Bagus! 🎈 *Soalan 1/7:* Berapakah umur anak anda sekarang? *(Pilih satu nombor di bawah)*\n\n"
                "👉 *[1]* 👶 0 – 12 Bulan\n"
                "👉 *[2]* 🧸 13 – 24 Bulan\n"
                "👉 *[3]* 🎨 25 – 36 Bulan\n"
                "👉 *[4]* 🎒 Lebih 3 Tahun"
            )
        elif body in ["2", "TIDAK SETUJU", "TIDAK"]:
            SESSIONS.pop(sender, None)
            resp.message("Terima kasih atas masa anda. Pendaftaran memerlukan persetujuan PDPA. Hantar *DAFTAR_KITAMAIN* jika ingin mencuba semula.")
        else:
            resp.message("⚠️ Sila balas *[1]* (✅ Saya Setuju) atau *[2]* (❌ Tidak Setuju).")

    elif step == "Q1_AGE_TRIAGE":
        age_map = {"1": "0 – 12 Bulan", "2": "13 – 24 Bulan", "3": "25 – 36 Bulan"}
        if body in age_map:
            user["age_bucket"] = age_map[body]
            user["step"] = "Q2_BASELINE"
            resp.message(
                "📖 *Soalan 2/7:* Dalam seminggu yang lepas, berapa kerap anda sempat bermain atau membaca buku bersama anak anda selama 10 minit? *(Tiada jawapan salah!)*\n\n"
                "👉 *[1]* 0 Kali Seminggu\n"
                "👉 *[2]* 1 – 2 Kali Seminggu\n"
                "👉 *[3]* 3+ Kali Seminggu"
            )
        elif body == "4":
            SESSIONS.pop(sender, None)
            resp.message(
                "🙏 Terima kasih atas minat anda! Maaf, Kotak Fizikal Kita Main dikhaskan untuk anak berumur *0 hingga 36 bulan (lahir 2023–2026)* sahaja.\n\n"
                "🎁 Muat turun *Panduan Aktiviti & Permainan Rumah PERCUMA* daripada YGG di sini:\n"
                "🔗 https://gg.org.my/kita-main-panduan-percuma"
            )
        else:
            resp.message("⚠️ Pilihan tidak sah. Sila balas *[1]*, *[2]*, *[3]*, atau *[4]* sahaja.")

    elif step == "Q2_BASELINE":
        baseline_map = {"1": "0 Kali Seminggu", "2": "1 – 2 Kali Seminggu", "3": "3+ Kali Seminggu"}
        if body in baseline_map:
            user["baseline"] = baseline_map[body]
            user["step"] = "Q3_NAME_IC"
            resp.message(
                "📝 *Soalan 3/7:* Sila taip *Nama Penuh Ibu/Bapa (Nama Penerima Kotak)* dan *No. IC (12 digit tanpa sengkang)* dipisahkan dengan koma.\n\n"
                "Contoh: *Siti Binti Ahmad, 920514105522*"
            )
        else:
            resp.message("⚠️ Sila balas *[1]*, *[2]*, atau *[3]* sahaja.")

    elif step == "Q3_NAME_IC":
        parts = [p.strip() for p in body.split(",")]
        ic_digits = re.sub(r"\D", "", parts[1]) if len(parts) > 1 else ""

        if len(parts) >= 2 and len(ic_digits) == 12:
            user["name"] = re.sub(r"[^A-Za-z\s'@/.-]", "", parts[0]).strip().upper()
            user["ic"] = ic_digits
            # Check if Parent IC is already registered -> Flag for Officer Review
            if ic_digits in REGISTERED_ICS:
                user["duplicate_ic_flag"] = True

            user["step"] = "Q4_CHILD_MYKID"
            resp.message(
                "👶 *Soalan 4/7:* Sila taip *12 digit No. MyKid anak* (atau tarikh lahir *HHBBTTTT* jika tiada MyKid).\n\n"
                "Contoh: *240815101122*"
            )
        else:
            user["ic_attempts"] = user.get("ic_attempts", 0) + 1
            if user["ic_attempts"] >= 3:
                user["name"] = body[:30].upper()
                user["ic"] = "SEMAKAN_MANUAL"
                user["step"] = "Q4_CHILD_MYKID"
                user["input_error_flag"] = True
                resp.message(
                    "ℹ️ Maklumat anda telah direkodkan untuk bantuan semakan pegawai.\n\n"
                    "👶 *Soalan 4/7:* Sila taip *12 digit No. MyKid anak* (atau tarikh lahir *HHBBTTTT* jika tiada MyKid).\n"
                    "Contoh: *240815101122*"
                )
            else:
                resp.message("⚠️ Maaf, nombor IC mestilah tepat 12 digit nombor sahaja. Sila taip semula (Contoh: *Siti Binti Ahmad, 920514105522*).")

    elif step == "Q4_CHILD_MYKID":
        is_valid, result_code, formatted_info, clean_mykid = validate_mykid_or_dob(body)
        if is_valid:
            user["child_mykid"] = formatted_info
            user["clean_mykid"] = clean_mykid
            user["kit_tier"] = result_code

            # Check if Child MyKid is already registered -> Flag for Officer Review
            if clean_mykid and clean_mykid in REGISTERED_MYKIDS:
                user["duplicate_mykid_flag"] = True

            user["step"] = "Q5_PROOF_TYPE"
            resp.message(
                "📸 *Soalan 5/7:* Apakah dokumen bukti kelayakan B40 yang paling mudah untuk anda muat naik sekarang?\n\n"
                "👉 *[1]* 📱 Tangkap Skrin STR\n"
                "👉 *[2]* 🏥 Tangkap Skrin MySalam\n"
                "👉 *[3]* 📄 Bil/Surat PPR"
            )
        elif result_code == "OVER_AGE":
            SESSIONS.pop(sender, None)
            resp.message(
                f"🙏 Maaf, berdasarkan tarikh lahir MyKid tersebut ({formatted_info}), umur anak melebihi had *36 bulan (lahir 2023–2026)*.\n\n"
                "🎁 Muat turun Panduan Aktiviti Rumah PERCUMA kami di sini: https://gg.org.my/kita-main-panduan-percuma"
            )
        else:
            resp.message("⚠️ Format MyKid/Tarikh Lahir tidak sah. Sila taip *12 digit No. MyKid* (Contoh: *240815101122*) atau *8 digit Tarikh Lahir HHBBTTTT* (Contoh: *15082024*).")

    elif step == "Q5_PROOF_TYPE":
        proof_map = {"1": "STR", "2": "MySalam", "3": "Bil/Surat PPR"}
        if body in proof_map:
            user["proof_type"] = proof_map[body]
            user["step"] = "Q5_EVIDENCE_UPLOAD"
            resp.message(
                f"✅ Pilihan dokumen: *{user['proof_type']}*\n\n"                "Sila muat naik *1 salinan dokumen atau tangkapan skrin (imej/PDF)* yang memaparkan *Nama/No. IC* anda dan *status penerima* (contoh: *\"LULUS\"*   *\"DITERIMA\"* atau *\"LAYAK\"*."

            )
        else:
            resp.message("⚠️ Sila pilih *[1]* (STR), *[2]* (MySalam), atau *[3]* (Bil/Surat PPR).")

    elif step == "Q5_EVIDENCE_UPLOAD":
        if num_media > 0:
            media_url = form_data.get("MediaUrl0", "")
            media_type = form_data.get("MediaContentType0", "image/jpeg")
            user["evidence_url"] = media_url

            ocr_status, ocr_reason = verify_evidence_document(
                media_url=media_url,
                media_type=media_type,
                proof_type=user.get("proof_type", "STR"),
                expected_ic=user.get("ic", ""),
                expected_name=user.get("name", "")
            )
            user["ocr_status"] = ocr_status
            user["ocr_reason"] = ocr_reason

            if user.get("address"):
                finalize_registration(sender, user, resp)
            else:
                user["step"] = "Q6_ADDRESS"
                resp.message(
                    "🚚 *Soalan 6/7:* Sila taip *Alamat Penuh & Poskod (5 digit)* untuk penghantaran kotak Kita Main.\n\n"
                    "Contoh: *Blok B-12-04, PPR Kerinchi, 59200 Kuala Lumpur*"
                )
        else:
            resp.message("📎 Sila muat naik *1 gambar tangkapan skrin (screenshot) atau fail PDF* dokumen bukti kelayakan anda.")

    elif step == "Q6_ADDRESS":
        postcode_match = re.search(r"\b\d{5}\b", body)
        if postcode_match:
            user["address"] = body.strip()
            user["postcode"] = postcode_match.group(0)
            norm_addr = normalize_address(user["address"])
            user["norm_address"] = norm_addr

            # Check if Delivery Address was already used multiple times -> Flag for Officer Review
            if norm_addr and norm_addr in REGISTERED_ADDRESSES:
                user["duplicate_address_flag"] = True

            user["step"] = "Q7_REFERRAL"
            resp.message(
                "🎁 *Soalan Terakhir (7/7) — Kod Rujukan Rakan (Pilihan):*\n"
                "Adakah anda mempunyai *Kod Rujukan (Referral Code)* daripada rakan atau jiran yang telah menyertai Kita Main?\n\n"
                "👉 Taip *Kod Rujukan* tersebut (Contoh: *KM-SA5522*)\n"
                "👉 Atau balas *[0]* / *TIADA* jika tiada."
            )
        else:
            resp.message("⚠️ Sila pastikan alamat anda mengandungi *Poskod 5 digit* yang sah (Contoh: *Blok B-12-04, PPR Kerinchi, 59200 Kuala Lumpur*).")

    elif step == "Q7_REFERRAL":
        raw_ref = body.strip().upper()
        if raw_ref in ["0", "TIADA", "NO", "NONE", "SKIP", "-"]:
            user["used_referral_code"] = ""
            user["referral_reward_triggered"] = False
        else:
            user["used_referral_code"] = raw_ref
            # Reward the referrer if valid (and prevent self-referral)
            referrer_sender = REFERRAL_CODE_TO_SENDER.get(raw_ref)
            if referrer_sender != sender:
                rewarded = reward_referrer_in_sheet_and_memory(raw_ref, user.get("name", "Keluarga Baharu"))
                user["referral_reward_triggered"] = rewarded
            else:
                user["referral_reward_triggered"] = False

        finalize_registration(sender, user, resp)

    elif step == "WAITLIST_OFFER_DECISION":
        if body in ["1", "YA", "SAHKAN", "SAHKAN ALAMAT"]:
            user["step"] = "COMPLETED"
            if sender in MASTER_DB:
                MASTER_DB[sender]["col_N_status"] = "CONFIRMED_FOR_DELIVERY"
                # If promoted from Waitlist (Sheet 3), move to Sheet 1 (Current Intake Window)
                sync_to_google_sheet(MASTER_DB[sender], target_tab=TAB_INTAKE, remove_from_tabs=[TAB_WAITLIST])
            resp.message(
                f"✅ *ALAMAT PENGHANTARAN DISAHKAN!*\n\n"
                f"Terima kasih *{user['name']}*. Kotak Kita Main (*{user.get('kit_tier', user.get('age_bucket'))}*) telah dikunci untuk penghantaran:\n"
                f"• *Nama Penerima:* {user['name']}\n"
                f"• *No. Telefon:* {user['phone']}\n"
                f"• *Alamat:* _{user['address']}_\n\n"
                f"🎁 *Kod Rujukan Anda:* *{user.get('referral_code', 'KM-YGG')}*\n"
                "_Kongsikan kod ini kepada rakan/jiran! Setiap rakan yang mendaftar menggunakan kod anda melayakkan anda menerima 1 Mainan Tambahan PERCUMA dalam kotak seterusnya._"
            )
        elif body in ["2", "TUKAR", "TUKAR ALAMAT"]:
            user["step"] = "UPDATE_ADDRESS"
            resp.message("✏️ Sila taip *Alamat Baharu & Poskod (5 digit)* anda sekarang:")
        elif body in ["3", "BATAL"] or body.upper() == "EXPIRE_48H":
            user["step"] = "EXPIRED_SKIPPED"
            if sender in MASTER_DB:
                MASTER_DB[sender]["col_N_status"] = "EXPIRED_SKIPPED"
                sync_to_google_sheet(MASTER_DB[sender])
            next_rank = user.get("waitlist_rank", 1) + 1
            resp.message(
                "⏰ *Tempoh 48 jam telah tamat / Slot dilepaskan.*\n"
                f"Slot ini telah diberikan secara automatik kepada keluarga seterusnya dalam senarai menunggu (*Giliran #{next_rank}*).\n\n"
                "Balas *AKTIFKAN* jika anda masih berminat untuk giliran kumpulan akan datang."
            )
        else:
            resp.message("Sila balas *[1]* ✅ Sahkan Alamat, *[2]* ✏️ Tukar Alamat, atau *[3]* ❌ Batal.")

    elif step == "UPDATE_ADDRESS":
        postcode_match = re.search(r"\b\d{5}\b", body)
        if postcode_match:
            user["address"] = body.strip()
            user["postcode"] = postcode_match.group(0)
            user["step"] = "COMPLETED"
            if sender in MASTER_DB:
                MASTER_DB[sender]["col_D_address"] = user["address"]
                MASTER_DB[sender]["col_N_status"] = "CONFIRMED_FOR_DELIVERY"
                sync_to_google_sheet(MASTER_DB[sender], target_tab=TAB_INTAKE, remove_from_tabs=[TAB_WAITLIST])
            resp.message(
                f"✅ *Alamat Baharu Dikemas Kini & Disahkan!*\n"
                f"• *Nama Penerima:* {user['name']}\n"
                f"• *No. Telefon:* {user['phone']}\n"
                f"• *Alamat:* _{user['address']}_\n\n"
                f"🎁 *Kod Rujukan Anda:* *{user.get('referral_code', 'KM-YGG')}*"
            )
        else:
            resp.message("⚠️ Sila pastikan alamat baharu mengandungi *5 digit Poskod*.")

    else:
        if body.upper() == "AKTIFKAN":
            user["step"] = "COMPLETED"
            if sender in MASTER_DB:
                MASTER_DB[sender]["col_N_status"] = "ELIGIBLE_WAITLIST"
                sync_to_google_sheet(MASTER_DB[sender], target_tab=TAB_WAITLIST)
            resp.message("✅ Nama anda telah diaktifkan semula dalam Senarai Menunggu Keutamaan!")
        else:
            resp.message(
                f"ℹ️ Permohonan Kita Main anda telah direkodkan (Kod Rujukan Anda: *{user.get('referral_code', 'KM-YGG')}*).\n"
                "Taip *OPEN_SLOT* untuk simulasi tawaran 48 jam, atau *RESET* untuk mula semula."
            )

    return Response(content=str(resp), media_type="application/xml")


# =====================================================================
# 6. FINALIZE REGISTRATION: DUPLICATE JSON FLAGS, REFERRAL & 3-SHEET ROUTING
# =====================================================================
def finalize_registration(sender: str, user: dict, resp: MessagingResponse):
    global APPROVED_ACTIVE_COUNT, WAITLIST_COUNTER

    ocr_status = user.get("ocr_status", "FLAGGED_REVIEW")
    ocr_reason = user.get("ocr_reason", "")

    # Generate unique referral code for this newly registered user
    if not user.get("referral_code"):
        own_code = generate_referral_code(user.get("name", ""), user.get("ic", ""), user.get("phone", ""))
        user["referral_code"] = own_code
        REFERRAL_CODE_TO_SENDER[own_code] = sender
    else:
        own_code = user["referral_code"]

    used_code = user.get("used_referral_code", "")
    reward_triggered = user.get("referral_reward_triggered", False)

    referral_note_msg = ""
    if used_code and reward_triggered:
        referral_note_msg = f"\n🎉 _Kod rujukan *{used_code}* berjaya disahkan! Rakan anda layak menerima 1 Mainan Tambahan pada kotak seterusnya._\n"
    elif used_code and not reward_triggered:
        referral_note_msg = f"\nℹ️ _Kod rujukan *{used_code}* telah direkodkan untuk semakan._\n"

    referral_share_footer = (
        "\n───────────────\n"
        f"🎁 *KOD RUJUKAN ANDA: {own_code}*\n"
        "Kongsikan kod ini kepada rakan/jiran yang mempunyai anak 0–3 tahun! "
        "Jika mereka mendaftar menggunakan kod ini, anda akan mendapat *1 Mainan Tambahan PERCUMA* di dalam kotak anda yang seterusnya! 🧸"
    )

    # Build list of triggered flag events for Column K (JSON)
    triggered_flags = []
    if user.get("duplicate_ic_flag"):
        triggered_flags.append("DUPLICATE_PARENT_IC")
    if user.get("duplicate_mykid_flag"):
        triggered_flags.append("DUPLICATE_CHILD_MYKID")
    if user.get("duplicate_address_flag"):
        triggered_flags.append("DUPLICATE_DELIVERY_ADDRESS")
    if user.get("input_error_flag"):
        triggered_flags.append("INPUT_FORMAT_ERROR_3X")
    if ocr_status != "VERIFIED":
        triggered_flags.append(f"OCR_FLAG:{ocr_reason.split('(')[0].strip()}")

    has_any_flag = len(triggered_flags) > 0

    flagged_json_payload = json.dumps({
        "is_flagged": has_any_flag,
        "flag_count": len(triggered_flags),
        "events": triggered_flags,
        "referral": {
            "own_code": own_code,
            "used_code": used_code,
            "referrer_rewarded": reward_triggered
        },
        "details": {
            "duplicate_parent_ic": bool(user.get("duplicate_ic_flag")),
            "duplicate_child_mykid": bool(user.get("duplicate_mykid_flag")),
            "duplicate_address": bool(user.get("duplicate_address_flag")),
            "input_error_3x": bool(user.get("input_error_flag")),
            "ocr_verdict": ocr_status,
            "ocr_note": ocr_reason
        }
    }, ensure_ascii=False)

    # Record current user's identifiers into registered sets for subsequent checks
    if user.get("ic") and user["ic"] != "SEMAKAN_MANUAL":
        REGISTERED_ICS.add(user["ic"])
    if user.get("clean_mykid"):
        REGISTERED_MYKIDS.add(user["clean_mykid"])
    if user.get("norm_address"):
        REGISTERED_ADDRESSES.add(user["norm_address"])

    # ROUTE 1: Flagged -> Sheet 2 (2_Under_Review_Officer)
    if has_any_flag:
        target_tab = TAB_REVIEW
        app_status = "FLAGGED_REVIEW"
        rank_str = "PENDING_OFFICER_REVIEW"
        user["step"] = "COMPLETED"
        default_wa_msg = (
            f"Salam {user['name']}, kami dari pasukan Program Kita Main (YGG). "
            f"Permohonan anda memerlukan pengesahan lanjut ({', '.join(triggered_flags)}). "
            "Boleh sahkan maklumat/dokumen anda di sini?"
        )
        resp.message(
            f"💛 *Terima kasih {user['name']}!*\n"
            "Permohonan anda telah diterima dan sedang disemak oleh pegawai kami dalam masa *24–48 jam*.\n"
            f"{referral_note_msg}\n"
            f"💡 Sambil menunggu pengesahan, luangkan 10 minit hari ini untuk bermain dan membaca bersama si manja ({user['age_bucket']})!"
            f"{referral_share_footer}"
        )

    # ROUTE 2: Clean & Within 800 Capacity -> Sheet 1 (1_Current_Intake_Window)
    elif APPROVED_ACTIVE_COUNT < BATCH_CAPACITY:
        APPROVED_ACTIVE_COUNT += 1
        target_tab = TAB_INTAKE
        app_status = "VERIFIED_CURRENT_INTAKE"
        rank_str = f"SLOT #{APPROVED_ACTIVE_COUNT}/{BATCH_CAPACITY}"
        user["step"] = "WAITLIST_OFFER_DECISION"
        default_wa_msg = (
            f"Tahniah {user['name']}! Kotak Kita Main ({user.get('kit_tier')}) untuk anak anda telah disahkan dalam Tetingkap Pengambilan Semasa ({rank_str})."
        )
        resp.message(
            f"🎉 *Tahniah {user['name']}! Kelayakan anda telah DISAHKAN.* ✅\n\n"
            f"Anda berjaya mendapat slot dalam *Tetingkap Pengambilan Semasa ({rank_str})* untuk menerima *Kotak Kita Main ({user.get('kit_tier', user.get('age_bucket'))})*!\n"
            f"{referral_note_msg}\n"
            f"💡 *Sambil menunggu kotak fizikal tiba, kami akan menghantar 1 tip permainan rumah \"Main 10 Minit\" setiap minggu khas untuk umur anak anda ({user['age_bucket']}) supaya perkembangan si manja terus berjalan!*"
            f"{referral_share_footer}\n\n"
            "───────────────\n"
            "🎁 *Pengesahan Penghantaran Kotak Kita Main:*\n"
            f"• *Nama Penerima:* {user['name']}\n"
            f"• *No. Telefon:* {user['phone']}\n"
            f"• *Alamat:* {user['address']}\n\n"
            "Sila sahkan alamat penghantaran anda dalam masa *48 JAM*:\n"
            "👉 Balas *[1]* ✅ Sahkan Alamat\n"
            "👉 Balas *[2]* ✏️ Tukar Alamat\n"
            "👉 Balas *[3]* ❌ Batal"
        )

    # ROUTE 3: Clean & Intake Cap (>800) Reached -> Sheet 3 (3_Eligible_Waitlist)
    else:
        WAITLIST_COUNTER += 1
        target_tab = TAB_WAITLIST
        app_status = "ELIGIBLE_WAITLIST"
        user["waitlist_rank"] = WAITLIST_COUNTER
        rank_str = f"Giliran #{WAITLIST_COUNTER}"
        user["step"] = "COMPLETED"
        default_wa_msg = (
            f"Tahniah {user['name']}! Satu slot Kotak Kita Main kini dibuka untuk anak anda ({rank_str}). Sila balas 1 untuk sahkan alamat penghantaran anda: {user['address']}"
        )
        resp.message(
            f"🎉 *Tahniah {user['name']}! Kelayakan anda telah DISAHKAN.* ✅\n\n"
            f"Kuota {BATCH_CAPACITY} Kotak Kita Main untuk Tetingkap Pengambilan Semasa telah penuh, tetapi nama anak anda telah dimasukkan secara automatik ke dalam *Senarai Menunggu Keutamaan ({rank_str})* untuk kumpulan seterusnya!\n"
            f"{referral_note_msg}\n"
            f"💡 *Sambil menunggu kotak fizikal tiba, kami akan menghantar 1 tip permainan rumah \"Main 10 Minit\" setiap minggu khas untuk umur anak anda ({user['age_bucket']}) supaya perkembangan si manja terus berjalan!*\n\n"
            "Kami akan menghantar makluman WhatsApp sebaik sahaja tetingkap penghantaran semasa selesai dan slot baharu dibuka."
            f"{referral_share_footer}"
        )

    col_m_formatted = format_col_m_baseline_and_referral(
        baseline=user.get("baseline", ""),
        own_code=own_code,
        used_code=used_code,
        ref_count=user.get("referral_count", 0)
    )

    record = {
        "sender_key": sender,
        "phone_raw": user["phone"],
        "col_A_timestamp_phone": f"{user['entry_time']} | {user['phone']}",
        "col_B_recipient_name": user.get("name", ""),
        "col_C_recipient_phone": user.get("phone", ""),
        "col_D_address": user.get("address", ""),
        "col_E_parent_ic": user.get("ic", ""),
        "col_F_child_mykid": user.get("child_mykid", ""),
        "col_G_age_bucket": f"{user.get('age_bucket', '')} | {user.get('kit_tier', '')}",
        "col_H_proof_type": user.get("proof_type", ""),
        "col_I_evidence_url": user.get("evidence_url", ""),
        "col_J_ocr_verdict": f"{ocr_status}: {ocr_reason}",
        "col_K_flagged_json": flagged_json_payload,
        "col_L_pdpa_consent": f"SETUJU ({user.get('consent_time', '')})",
        "col_M_baseline": col_m_formatted,
        "col_N_status": app_status,
        "col_O_rank": rank_str,
        "col_P_officer_decision": "PENDING" if has_any_flag else "AUTO_PASS",
        "col_Q_custom_msg": "",
        "col_R_wa_link": build_wa_hyperlink_formula(user["phone"], default_wa_msg)
    }
    MASTER_DB[sender] = record
    sync_to_google_sheet(record, target_tab=target_tab)


# =====================================================================
# 7. OFFICER ACTION ENDPOINT (PASS -> MOVE TO SHEET 1, FAIL -> WA.ME LINK)
# =====================================================================
@app.post("/officer-action")
async def officer_action(
    sender_key: str = Form(...),
    action: str = Form(...),
    custom_message: str = Form("")
):
    global APPROVED_ACTIVE_COUNT, WAITLIST_COUNTER
    clean_phone = sender_key.replace("whatsapp:", "").strip()
    if not sender_key.startswith("whatsapp:"):
        sender_key = f"whatsapp:{clean_phone}"

    record = MASTER_DB.get(sender_key)
    user = SESSIONS.get(sender_key, {})
    parent_name = record["col_B_recipient_name"] if record else user.get("name", "Ibu/Bapa")
    address_val = record["col_D_address"] if record else user.get("address", "Alamat Anda")

    action_upper = action.strip().upper()

    # 1. OFFICER SELECTS "PASS" IN SHEET 2 -> IMMEDIATELY MOVE TO SHEET 1 (OR SHEET 3 IF >800)
    if action_upper in ["PASS", "APPROVE_MANUAL"]:
        if APPROVED_ACTIVE_COUNT < BATCH_CAPACITY:
            APPROVED_ACTIVE_COUNT += 1
            dest_tab = TAB_INTAKE
            new_status = "VERIFIED_OFFICER_PASS"
            rank_str = f"SLOT #{APPROVED_ACTIVE_COUNT}/{BATCH_CAPACITY}"
            default_msg = (
                f"🎉 Tahniah {parent_name}! Semakan pegawai selesai dan permohonan Kita Main anda telah DILULUSKAN ({rank_str}). "
                f"Sila balas [1] untuk sahkan alamat penghantaran anda ({address_val})."
            )
            if user:
                user["step"] = "WAITLIST_OFFER_DECISION"
        else:
            WAITLIST_COUNTER += 1
            dest_tab = TAB_WAITLIST
            new_status = "ELIGIBLE_WAITLIST"
            rank_str = f"Giliran #{WAITLIST_COUNTER}"
            default_msg = (
                f"🎉 Tahniah {parent_name}! Semakan pegawai selesai dan permohonan Kita Main anda telah DILULUSKAN. "
                f"Anak anda kini berada dalam Senarai Menunggu Keutamaan ({rank_str})."
            )
            if user:
                user["waitlist_rank"] = WAITLIST_COUNTER

        msg_to_send = custom_message.strip() if custom_message.strip() else default_msg
        wa_url = build_wa_me_url(clean_phone, msg_to_send)

        if record:
            record["col_N_status"] = new_status
            record["col_O_rank"] = rank_str
            record["col_P_officer_decision"] = "PASS"
            record["col_Q_custom_msg"] = msg_to_send
            record["col_R_wa_link"] = build_wa_hyperlink_formula(clean_phone, msg_to_send, "✅ Chat Kelulusan")
            # Move row out of Sheet 2 (Under Review) into Sheet 1 (Intake) or Sheet 3 (Waitlist)
            sync_to_google_sheet(record, target_tab=dest_tab, remove_from_tabs=[TAB_REVIEW])

        send_outbound_whatsapp(sender_key, msg_to_send)
        return JSONResponse({
            "ok": True,
            "status": "MOVED_TO_" + dest_tab,
            "wa_url": wa_url,
            "sent_message": msg_to_send
        })

    # 2. OFFICER SELECTS "FAIL" OR "REQUEST_REUPLOAD" -> UPDATE LINK IN SHEET 2 FOR MANUAL WA CONTACT
    elif action_upper in ["FAIL", "REQUEST_REUPLOAD"]:
        default_msg = (
            f"Salam {parent_name}, makluman daripada pegawai Program Kita Main (YGG): "
            "Permohonan anda tidak dapat diluluskan buat masa ini kerana dokumen/maklumat tidak lengkap. "
            "Sila balas mesej ini dengan dokumen bukti yang sah jika ingin membuat rayuan."
        )
        msg_to_send = custom_message.strip() if custom_message.strip() else default_msg
        wa_url = build_wa_me_url(clean_phone, msg_to_send)

        if record:
            record["col_N_status"] = "REJECTED_OFFICER_FAIL" if action_upper == "FAIL" else "REQUEST_REUPLOAD"
            record["col_P_officer_decision"] = "FAIL"
            record["col_Q_custom_msg"] = msg_to_send
            record["col_R_wa_link"] = build_wa_hyperlink_formula(clean_phone, msg_to_send, "⚠️ Klik Hubungi Pelanggan (FAIL)")
            sync_to_google_sheet(record, target_tab=TAB_REVIEW)
        if user:
            user["step"] = "Q5_EVIDENCE_UPLOAD"

        send_outbound_whatsapp(sender_key, msg_to_send)
        return JSONResponse({
            "ok": True,
            "status": "FAILED_FLAGGED",
            "wa_url": wa_url,
            "sent_message": msg_to_send
        })

    # 3. OFFICER TYPES CUSTOM MESSAGE IN COLUMN Q
    elif action_upper == "CUSTOM_MESSAGE":
        msg_to_send = custom_message.strip()
        wa_url = build_wa_me_url(clean_phone, msg_to_send)
        if record:
            record["col_Q_custom_msg"] = msg_to_send
            record["col_R_wa_link"] = build_wa_hyperlink_formula(clean_phone, msg_to_send, "💬 Klik Hantar Mesej Pegawai")
            sync_to_google_sheet(record)
        send_outbound_whatsapp(sender_key, msg_to_send)
        return JSONResponse({
            "ok": True,
            "status": "CUSTOM_LINK_READY",
            "wa_url": wa_url,
            "sent_message": msg_to_send
        })

    return JSONResponse({"ok": False, "error": "Invalid action"}, status_code=400)


# =====================================================================
# 8. PROMOTE WAITLIST TO CURRENT INTAKE WINDOW (/promote-waitlist)
# =====================================================================
@app.post("/promote-waitlist")
async def promote_waitlist_when_intake_cleared():
    """Moves Eligible-Waitlist rows from Sheet 3 into Sheet 1 when current intake window is cleared."""
    global APPROVED_ACTIVE_COUNT
    sh = get_spreadsheet()
    if not sh:
        return JSONResponse({"ok": False, "error": "Sheets offline"}, status_code=500)

    ws_intake = sh.worksheet(TAB_INTAKE)
    ws_waitlist = sh.worksheet(TAB_WAITLIST)
    waitlist_rows = ws_waitlist.get_all_values()[1:]

    promoted_count = 0
    for row in waitlist_rows:
        if not any(row):
            continue
        if APPROVED_ACTIVE_COUNT >= BATCH_CAPACITY:
            break
        APPROVED_ACTIVE_COUNT += 1
        row[13] = "PROMOTED_FROM_WAITLIST"
        row[14] = f"SLOT #{APPROVED_ACTIVE_COUNT}/{BATCH_CAPACITY}"
        ws_intake.append_row(row, value_input_option="USER_ENTERED")
        promoted_count += 1

    if promoted_count > 0:
        # Clear promoted rows from Sheet 3
        ws_waitlist.resize(rows=1)
        ws_waitlist.resize(rows=1000)

    return JSONResponse({"ok": True, "promoted_to_current_intake": promoted_count})