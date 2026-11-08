import time
import io
import re
from PIL import Image, ImageDraw
import main

# Disable outbound network calls to Google Sheets and Twilio for local stress test
main.sync_to_google_sheet = lambda *args, **kwargs: None
main.send_outbound_whatsapp = lambda *args, **kwargs: True

def create_sample_str_slip(name="SITI BINTI AHMAD", ic="920514105522", status="LULUS"):
    img = Image.new("RGB", (600, 300), color="white")
    draw = ImageDraw.Draw(img)
    text = (
        f"SUMBANGAN TUNAI RAHMAH (STR)\n"
        f"STATUS PERMOHONAN: {status}\n"
        f"NAMA: {name}\n"
        f"NO KAD PENGENALAN: {ic}\n"
        f"KATEGORI: ISI RUMAH B40"
    )
    draw.text((30, 40), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()

def run_stress_test():
    print("=" * 65)
    print("🚀 STRESS TEST: OCR + WHATSAPP INTAKE PIPELINE")
    print(f"Active OCR Engine: {main.OCR_ENGINE}")
    print("=" * 65)

    # 1. Direct Single Scan Verification
    print("\n[Step 1] Testing Single Image OCR Verification...")
    sample_bytes = create_sample_str_slip()
    main.download_twilio_media = lambda url: sample_bytes

    t0 = time.time()
    verdict, reason = main.verify_evidence_document(
        media_url="https://mock.test/evidence.jpg",
        media_type="image/jpeg",
        proof_type="STR",
        expected_ic="920514105522",
        expected_name="SITI BINTI AHMAD"
    )
    t_single = time.time() - t0
    print(f"  Result : {verdict} ({reason})")
    print(f"  Latency: {t_single*1000:.1f}ms")
    assert verdict == "VERIFIED", f"Failed: {verdict}"
    print("  -> Passed!")

    # 2. High-Frequency OCR Stress (50 iterations)
    print("\n[Step 2] High-Frequency OCR Stress Test (50 Consecutive Scans)...")
    t_start = time.time()
    for i in range(1, 51):
        v, _ = main.verify_evidence_document(
            media_url="https://mock.test/evidence.jpg",
            media_type="image/jpeg",
            proof_type="STR",
            expected_ic="920514105522",
            expected_name="SITI BINTI AHMAD"
        )
        assert v == "VERIFIED"
        if i % 10 == 0:
            print(f"  Processed {i}/50 scans...")

    t_total = time.time() - t_start
    avg_ms = (t_total / 50) * 1000
    print(f"  Total Time: {t_total:.2f}s | Average Speed: {avg_ms:.1f}ms per scan")
    print("  -> Passed!")

    # 3. Mismatch / Edge Cases (OCR Fraud & Tamper Detection)
    print("\n[Step 3] Testing OCR Mismatch / Fraud Detection Edge Cases...")
    
    # Tampered Name
    v_mismatch, r_mismatch = main.verify_evidence_document(
        media_url="https://mock.test/evidence.jpg",
        media_type="image/jpeg",
        proof_type="STR",
        expected_ic="999999999999",
        expected_name="ALI BIN ABU"
    )
    print(f"  Mismatch Check Result: {v_mismatch} ({r_mismatch})")
    assert v_mismatch == "FLAGGED_REVIEW"

    # Blank/Corrupted Image
    blank_img = Image.new("RGB", (300, 100), color="white")
    b_buf = io.BytesIO()
    blank_img.save(b_buf, format="JPEG")
    main.download_twilio_media = lambda url: b_buf.getvalue()
    v_blank, r_blank = main.verify_evidence_document(
        media_url="https://mock.test/evidence.jpg",
        media_type="image/jpeg",
        proof_type="STR",
        expected_ic="920514105522",
        expected_name="SITI BINTI AHMAD"
    )
    print(f"  Blank Image Result   : {v_blank} ({r_blank})")
    assert v_blank == "FLAGGED_REVIEW"
    print("  -> Passed!")

    print("\n" + "=" * 65)
    print("🎉 ALL STRESS TESTS COMPLETED SUCCESSFULLY!")
    print(f"✅ Tesseract Engine is ready, responsive, and robust ({avg_ms:.1f}ms/op).")
    print("=" * 65)

if __name__ == "__main__":
    run_stress_test()
