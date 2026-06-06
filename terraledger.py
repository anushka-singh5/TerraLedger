# TerraLedger backend — FastAPI + 5 AI verification modules + on-chain oracle signer

# standard library
import collections
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import pickle
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# third-party
from fastapi.responses import FileResponse
import numpy as np
import pandas as pd
import requests
import fitz                          # PyMuPDF
import pytesseract
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from pydantic import BaseModel
from shapely.geometry import Polygon
from shapely.validation import make_valid
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
import uvicorn

# optional: spacy nlp
try:
    import spacy as _spacy
    _nlp = _spacy.load("en_core_web_sm")
    SPACY_AVAILABLE = True
except Exception:
    _nlp = None
    SPACY_AVAILABLE = False

# optional: web3 for on-chain oracle calls
try:
    from web3 import Web3
    from eth_account import Account as EthAccount
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

load_dotenv()

# logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("terraledger")

# constants & tunables
MIN_SCORE          = 70
HARD_FAIL_OVERLAP  = 20.0       # GPS overlap % that triggers hard-fail
WARN_OVERLAP       = 5.0        # GPS overlap % that adds a flag
MAX_DOC_DIST_KM    = 50.0       # max km between doc GPS and project centroid

NASA_BASE_URL      = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
NASA_DAYS          = 90         # total satellite lookback window
NASA_CHUNK_DAYS    = 5          # FIRMS free API hard limit per request (max 5)
NASA_SOURCE        = "VIIRS_SNPP_NRT"   # 375m VIIRS, near-real-time

BIOMES             = ["tropical", "temperate", "boreal", "wetland", "other"]
PROJECT_TYPES      = ["forest", "agriculture", "renewable", "methane", "other"]
# Realistic 5th-95th percentile ranges from Verra registry projects.
# NOT the physical maximum — that lives in BIOME_HARD_CAPS.
BIOME_RANGES: Dict[str, Tuple[int, int]] = {
    "tropical":  (3,   120),
    "temperate": (2,    90),
    "boreal":    (1,    60),
    "wetland":   (8,   280),
    "other":     (1,    80),
}

# Realistic median tCO2/ha per biome (log-normal center for training data)
BIOME_MEDIANS: Dict[str, float] = {
    "tropical":  28.0,
    "temperate": 22.0,
    "boreal":    15.0,
    "wetland":   65.0,
    "other":     12.0,
}

# literature-grounded median tco2/ha by (project type × biome)
# Project type matters as much as biome: a REDD+ forest, an agroforestry plot and
# a renewable-energy avoidance project have very different per-hectare profiles.
# Values are credit-yield medians derived from published Verra/Gold Standard
# project ranges and IPCC AR6 WGIII sequestration rates (tCO2e/ha, annualised then
# typical crediting-period scaled). Used to generate a realistic training prior so
# the anomaly ensemble doesn't mis-flag legitimate type/biome combinations.
# A real registry export (data/verra_projects.csv) overrides this when present.
TYPE_BIOME_MEDIANS: Dict[Tuple[str, str], float] = {
    # forest / REDD+ / afforestation — highest per-ha sequestration
    ("forest", "tropical"): 30.0, ("forest", "temperate"): 22.0,
    ("forest", "boreal"): 14.0,   ("forest", "wetland"): 70.0,  ("forest", "other"): 18.0,
    # agriculture / agroforestry / soil carbon — lower, slower
    ("agriculture", "tropical"): 9.0,  ("agriculture", "temperate"): 6.0,
    ("agriculture", "boreal"): 4.0,    ("agriculture", "wetland"): 12.0, ("agriculture", "other"): 6.0,
    # renewable energy — avoidance, measured per project not per ha; low per-ha
    ("renewable", "tropical"): 4.0,  ("renewable", "temperate"): 4.0,
    ("renewable", "boreal"): 3.0,    ("renewable", "wetland"): 4.0,    ("renewable", "other"): 4.0,
    # methane capture (landfill/livestock) — concentrated, low land footprint
    ("methane", "tropical"): 5.0,  ("methane", "temperate"): 5.0,
    ("methane", "boreal"): 4.0,    ("methane", "wetland"): 8.0,       ("methane", "other"): 5.0,
    # other
    ("other", "tropical"): 12.0, ("other", "temperate"): 9.0,
    ("other", "boreal"): 6.0,    ("other", "wetland"): 20.0,          ("other", "other"): 8.0,
}

MODEL_PATH = Path("data/isolation_forest.pkl")
DATA_PATH  = Path("data/verra_projects.csv")
VERRA_VOLUME_STATS_PATH = Path("data/verra_type_volume_stats.json")

# Real per-project-type annual credit-volume distribution, built from 1,400+ real
# Verra registry projects (scripts/build_verra_stats.py). Used as a complementary,
# hectare-independent "volume realism" check: a claim whose magnitude is far above
# what real projects of its type ever produce is flagged. Loaded once at import.
def _load_verra_volume_stats() -> Dict[str, Dict]:
    if VERRA_VOLUME_STATS_PATH.exists():
        try:
            return json.loads(VERRA_VOLUME_STATS_PATH.read_text())
        except Exception:
            pass
    return {}

_VERRA_VOL_STATS = _load_verra_volume_stats()
PDF_DIR    = Path("data/reports")

OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")

GPS_PATTERNS = [
    r"(-?\d{1,3}\.\d{4,})\s*[,°]\s*(-?\d{1,3}\.\d{4,})",
    r"(\d{1,3})°(\d{1,2})'(\d{1,2})\"([NS])\s+(\d{1,3})°(\d{1,2})'(\d{1,2})\"([EW])",
    r"[Ll]at(?:itude)?[:\s]+(-?\d{1,3}\.\d+).*?[Ll]on(?:gitude)?[:\s]+(-?\d{1,3}\.\d+)",
]

# Anomaly ensemble — populated once at first request
_ensemble:        List[IsolationForest] = []
_ensemble_scaler: Optional[StandardScaler] = None
_ensemble_stats:  Dict[str, Dict]       = {}   # per-biome {"mean": x, "std": y}
_ensemble_ready:  bool                  = False

# rate-limit state (in-memory; short windows, fine to reset on restart)
_rl_attempts:   Dict[str, List[float]] = collections.defaultdict(list)  # wallet → timestamps
_rl_failures:   Dict[str, List[float]] = collections.defaultdict(list)  # wallet → fail times
_rl_cooldown:   Dict[str, float]       = {}                              # wallet → expiry ts
_rl_anomaly_q:  Dict[str, List[float]] = collections.defaultdict(list)  # for probe detection
# Fraud strikes PERSIST across restart (a ban must not be wiped by a reboot)
_fraud_counts:  Dict[str, int]         = collections.defaultdict(int)    # wallet → fraud count
REPUTATION_PATH = Path("data/reputation.json")

def _save_fraud_counts() -> None:
    try:
        REPUTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPUTATION_PATH.write_text(json.dumps({k: v for k, v in _fraud_counts.items() if v}))
    except Exception:
        pass

def _load_fraud_counts() -> None:
    try:
        if REPUTATION_PATH.exists():
            for k, v in json.loads(REPUTATION_PATH.read_text()).items():
                _fraud_counts[k] = int(v)
    except Exception:
        pass

_load_fraud_counts()

MAX_HOURLY     = 5      # max submissions per hour per wallet
FAIL_WINDOW    = 600    # 10 min window for failure tracking
FAIL_COOLDOWN  = 1800   # 30 min cooldown after repeated failures
PROBE_WINDOW   = 5      # last N anomaly scores checked for boundary probing

# request / response models
class VerificationResult(BaseModel):
    project_id:              str
    submitter:               str
    verdict:                 str           # "PASS" | "FAIL"
    total_score:             int
    scores:                  Dict[str, int]
    flags:                   List[str]
    owner_name:              str
    gps_overlap_pct:         float
    conflicting_project_ids: List[str]
    anomaly_score:           float
    fire_count:              int
    permanence_risk:         str
    report_text:             str
    report_ipfs_cid:         Optional[str]
    report_pdf_path:         Optional[str]
    on_chain_tx:             Optional[str]
    processing_time_ms:      int
    should_mint:             bool
    reject_reason:           Optional[str]
    # Reputation & anti-abuse fields
    submitter_reputation:    str           # "clean" | "flagged" | "banned"
    submitter_fraud_history: int           # past fraud count for this wallet
    rate_limit_remaining:    int           # submissions left this hour

# module 1 — gps overlap detector
def check_gps_overlap(
    new_polygon: List[List[float]],
    registered:  List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compare a new project polygon against all registered polygons on-chain.

    Returns a result dict with:
        hard_fail (bool), score (int, 0-30),
        overlap_pct (float), conflicting_ids (list), verdict, reason
    """
    if len(new_polygon) < 3:
        return {
            "hard_fail":       True,
            "score":           0,
            "overlap_pct":     0.0,
            "conflicting_ids": [],
            "verdict":         "INVALID_POLYGON",
            "reason":          "Polygon must have at least 3 GPS points.",
        }

    try:
        # GeoJSON lon/lat → shapely expects (lon, lat)
        new_poly = make_valid(Polygon([(p[1], p[0]) for p in new_polygon]))
    except Exception as exc:
        return {
            "hard_fail":       True,
            "score":           0,
            "overlap_pct":     0.0,
            "conflicting_ids": [],
            "verdict":         "INVALID_POLYGON",
            "reason":          str(exc),
        }

    max_overlap = 0.0
    conflicting = []

    for entry in registered:
        try:
            reg_poly = make_valid(Polygon([(p[1], p[0]) for p in entry["polygon"]]))
            if not new_poly.intersects(reg_poly):
                continue
            inter = new_poly.intersection(reg_poly).area
            # Measure overlap against the SMALLER polygon, not just the new one.
            # Otherwise a fraudster drops a huge polygon over an existing small
            # claim and the new-area ratio stays tiny — this catches that.
            smaller = min(new_poly.area, reg_poly.area) or new_poly.area
            overlap_pct = (inter / smaller) * 100 if smaller else 0.0
            if overlap_pct > WARN_OVERLAP:
                conflicting.append(entry["project_id"])
                max_overlap = max(max_overlap, overlap_pct)
        except Exception:
            continue

    if max_overlap >= HARD_FAIL_OVERLAP:
        return {
            "hard_fail":       True,
            "score":           0,
            "overlap_pct":     round(max_overlap, 2),
            "conflicting_ids": conflicting,
            "verdict":         "DUPLICATE_LAND",
            "reason":          (
                f"GPS overlap {max_overlap:.1f}% with project(s): "
                f"{', '.join(conflicting)}"
            ),
        }

    if max_overlap == 0:
        score = 30
    elif max_overlap < 5:
        score = 25
    elif max_overlap < 10:
        score = 15
    else:
        score = 5

    return {
        "hard_fail":       False,
        "score":           score,
        "overlap_pct":     round(max_overlap, 2),
        "conflicting_ids": conflicting,
        "verdict":         "PASS" if max_overlap < WARN_OVERLAP else "WARNING",
        "reason":          (
            "No significant GPS overlap detected."
            if max_overlap < WARN_OVERLAP
            else f"Minor overlap ({max_overlap:.1f}%) with {len(conflicting)} project(s)."
        ),
    }

# module 2 — ownership doc parser + forensics
# Two passes. First "is it consistent?" — owner name + GPS present, and the GPS
# actually matches where the project says it is. Then the harder question, "is it
# real?", which is four separate checks:
#   1. Forgery   — was the PDF spat out by reportlab/Word/Canva/ChatGPT? Real
#                  deeds come off a scanner or a govt system, not a PDF library.
#   2. Reuse     — same deed (or same owner) already used for a different GPS.
#                  One deed, many forests = someone's recycling paperwork.
#   3. Structure — does it actually read like a deed (reg number, registry terms)?
#   4. Tampering — Error Level Analysis on uploaded photos.

# Software that GENERATES PDFs — a real land deed is never produced by these.
GENERATED_PDF_PRODUCERS = [
    "reportlab", "wkhtmltopdf", "tcpdf", "dompdf", "jspdf", "pdfkit",
    "microsoft word", "word", "libreoffice", "openoffice", "writer",
    "google docs", "canva", "chatgpt", "openai", "pillow", "matplotlib",
    "cairo", "skia", "chrome", "chromium", "headless", "puppeteer", "latex",
    "powerpoint", "pages", "figma", "photoshop", "gimp", "illustrator",
]

# Producers consistent with a genuine scanned / authority-issued document.
SCANNER_PRODUCERS = [
    "adobe", "acrobat", "scan", "scanner", "epson", "canon", "hp scan",
    "xerox", "kyocera", "ricoh", "naps2", "camscanner", "brother",
]

# Terminology that appears in real land-registry documents.
REGISTRY_KEYWORDS = [
    "registry", "registrar", "cadastr", "title deed", "land title",
    "survey number", "parcel", "folio", "khasra", "khatauni", "patta",
    "sub-registrar", "sub registrar", "encumbrance", "mutation",
    "deed of", "conveyance", "freehold", "leasehold", "torrens",
]

# Official registration / parcel number, e.g. "Reg No: BRN-2025-03821"
REG_NUMBER_PATTERN = (
    r"(?:reg(?:istration)?|parcel|folio|deed|title|survey)\s*"
    r"(?:no|number|#|id)?[.:\s]*([A-Z0-9][A-Z0-9\-/]{4,})"
)

DOC_REGISTRY_PATH = Path("data/document_registry.json")
ELA_TAMPER_THRESHOLD = 0.65   # max normalized ELA difference before flagging

DOC_ACCESS_PATH = Path("data/extended_documents.json")

def _load_doc_registry() -> Dict[str, Any]:
    # load the persistent document-fingerprint registry (for reuse detection)

    if DOC_REGISTRY_PATH.exists():
        try:
            return json.loads(DOC_REGISTRY_PATH.read_text())
        except Exception:
            pass
    return {"by_hash": {}, "by_owner": {}}

def _save_extended_doc(project_id: str, project_name: str, doc_result: Dict[str, Any]) -> None:
    # persist a REDACTED extended-documentation record for a verified project

    try:
        store: Dict[str, Any] = {}
        if DOC_ACCESS_PATH.exists():
            store = json.loads(DOC_ACCESS_PATH.read_text())
        owner = doc_result.get("owner_name", "Unknown")
        # Redact: keep first name + last initial only (PII minimisation)
        redacted_owner = owner
        if owner and owner not in ("Unknown", "Unreadable"):
            bits = owner.split()
            redacted_owner = bits[0] + (" " + bits[-1][0] + "." if len(bits) > 1 else "")
        store[project_id] = {
            "project_name":   project_name,
            "owner_redacted": redacted_owner,
            "doc_gps":        doc_result.get("doc_gps"),
            "gps_match":      doc_result.get("gps_match"),
            "authenticity":   doc_result.get("authenticity"),
            "doc_hash":       doc_result.get("doc_hash", ""),
            "saved_at":       datetime.utcnow().isoformat(),
        }
        DOC_ACCESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOC_ACCESS_PATH.write_text(json.dumps(store, indent=2))
    except Exception as exc:
        log.warning("extended-doc save failed: %s", str(exc)[:80])

def _save_doc_registry(reg: Dict[str, Any]) -> None:
    DOC_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_REGISTRY_PATH.write_text(json.dumps(reg, indent=2))

def _record_doc_fingerprint(doc_hash: str, owner: str, doc_gps, project_id: str) -> None:
    # record a deed's fingerprint for future reuse detection — ONLY after it has

    if not doc_hash:
        return
    try:
        reg = _load_doc_registry()
        if doc_hash in reg.get("by_hash", {}):
            return   # already recorded (idempotent)
        reg["by_hash"][doc_hash] = {
            "project_id": project_id, "owner": owner,
            "gps": doc_gps, "ts": datetime.utcnow().isoformat(),
        }
        if owner and owner not in ("Unknown", "Unreadable"):
            reg["by_owner"].setdefault(owner.lower().strip(), []).append(
                {"project_id": project_id, "gps": doc_gps}
            )
        _save_doc_registry(reg)
    except Exception as exc:
        log.warning("doc fingerprint record failed: %s", str(exc)[:80])

def _error_level_analysis(image_bytes: bytes) -> float:
    # error Level Analysis: re-compress the image at known JPEG quality and

    try:
        from PIL import Image, ImageChops
        orig = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        buf  = io.BytesIO()
        orig.save(buf, "JPEG", quality=90)
        buf.seek(0)
        resaved = Image.open(buf).convert("RGB")
        diff    = ImageChops.difference(orig, resaved)
        extrema = diff.getextrema()           # per-channel (min, max)
        max_d   = max(ch[1] for ch in extrema)
        return max_d / 255.0
    except Exception as exc:
        log.warning("ELA failed: %s", exc)
        return 0.0

def check_document_authenticity(
    file_bytes: bytes,
    filename:   str,
    text:       str,
    owner:      str,
    doc_gps:    Optional[List[float]],
    project_id: str,
) -> Dict[str, Any]:
    """
    Layer B — verify the document is genuine, not just consistent.
    Returns authenticity multiplier (0-1), hard-fail flag, and reasons.
    """
    fname        = (filename or "").lower()
    flags:       List[str] = []
    authenticity = 1.0
    is_forgery   = False
    is_reused    = False   # → hard fail

    # ── Check 1: PDF forgery (metadata) ───────────────────────────────────────
    if fname.endswith(".pdf"):
        try:
            doc      = fitz.open(stream=file_bytes, filetype="pdf")
            producer = (doc.metadata.get("producer") or "").lower()
            creator  = (doc.metadata.get("creator")  or "").lower()
            doc.close()
            meta = f"{producer} {creator}".strip()

            if any(g in meta for g in GENERATED_PDF_PRODUCERS):
                is_forgery    = True
                authenticity -= 0.6
                flags.append(
                    f"FORGED_DOCUMENT: PDF generated by '{(producer or creator)[:40]}' "
                    f"— genuine deeds are scanned or issued by a registry, not generated"
                )
            elif meta and not any(s in meta for s in SCANNER_PRODUCERS):
                authenticity -= 0.2
                flags.append(f"UNVERIFIED_SOURCE: PDF producer '{producer[:30]}' is not a known scanner/registry")
        except Exception:
            pass

    # ── Check 4: Image tampering (ELA) ────────────────────────────────────────
    elif fname.endswith((".jpg", ".jpeg", ".png")):
        ela = _error_level_analysis(file_bytes)
        if ela > ELA_TAMPER_THRESHOLD:
            authenticity -= 0.4
            flags.append(f"IMAGE_TAMPERING: Error Level Analysis detected edited regions (ELA={ela:.2f})")

    # ── Check 3: Structural authenticity ──────────────────────────────────────
    low            = text.lower()
    has_registry   = any(kw in low for kw in REGISTRY_KEYWORDS)
    has_reg_number = bool(re.search(REG_NUMBER_PATTERN, text, re.IGNORECASE))
    if not has_registry:
        authenticity -= 0.2
        flags.append("NO_REGISTRY_MARKERS: document lacks land-registry terminology")
    if not has_reg_number:
        authenticity -= 0.15
        flags.append("NO_REGISTRATION_NUMBER: no official registration/parcel number found")

    # ── Check 2: Reuse detection (persistent fingerprint registry) ────────────
    doc_hash = hashlib.sha256(file_bytes).hexdigest()
    registry = _load_doc_registry()

    prev = registry["by_hash"].get(doc_hash)
    if prev and prev.get("project_id") != project_id:
        is_reused = True
        flags.append(
            f"DOCUMENT_REUSE: this exact document was already submitted for "
            f"project {prev['project_id']} — one deed cannot back two projects"
        )

    if owner and owner not in ("Unknown", "Unreadable"):
        okey      = owner.lower().strip()
        prev_locs = registry["by_owner"].get(okey, [])
        for loc in prev_locs:
            if loc.get("project_id") != project_id and doc_gps and loc.get("gps"):
                dist = _haversine(doc_gps, loc["gps"])
                if dist > 500:   # same owner, 500+ km apart = suspicious multi-claim
                    authenticity -= 0.2
                    flags.append(
                        f"OWNER_MULTI_CLAIM: '{owner}' already claimed land {dist:.0f} km "
                        f"away (project {loc['project_id']})"
                    )
                    break

    # NOTE: the fingerprint is recorded ONLY after the deed actually backs a
    # successfully-minted credit (see _record_doc_fingerprint, called from /verify
    # on a PASS + on-chain mint). Recording here — during parsing — was a bug: a
    # failed/rejected/transient-error submission would "consume" the deed and
    # falsely flag a legitimate retry as DOCUMENT_REUSE.

    return {
        "authenticity": round(max(0.0, min(1.0, authenticity)), 2),
        "is_forgery":   is_forgery,
        "is_reused":    is_reused,
        "doc_hash":     doc_hash[:16],
        "flags":        flags,
    }

def parse_ownership_document(
    file_bytes: bytes,
    filename:   str,
    polygon:    List[List[float]],
    project_id: str = "unknown",
) -> Dict[str, Any]:
    """
    Layer A (consistency) + Layer B (authenticity).
    Hard-fails on GPS mismatch OR document reuse.
    Final score = 25 × consistency_confidence × authenticity_multiplier.
    """
    text = _extract_text(file_bytes, filename)

    if not text or len(text.strip()) < 20:
        return {
            "hard_fail":  False,
            "score":      0,
            "owner_name": "Unreadable",
            "doc_gps":    None,
            "gps_match":  False,
            "confidence": 0.0,
            "authenticity": 0.0,
            "auth_flags": ["UNREADABLE_DOCUMENT"],
            "reason":     "Document could not be read (check file format).",
        }

    owner     = _extract_owner(text)
    doc_gps   = _extract_gps_from_text(text)
    centroid  = _polygon_centroid(polygon)
    dist_km   = None
    gps_match = False

    if doc_gps and centroid:
        dist_km   = _haversine(doc_gps, centroid)
        gps_match = dist_km <= MAX_DOC_DIST_KM

        if not gps_match:
            return {
                "hard_fail":  True,
                "score":      0,
                "owner_name": owner,
                "doc_gps":    doc_gps,
                "gps_match":  False,
                "confidence": 0.0,
                "authenticity": 0.0,
                "auth_flags": [],
                "reason":     (
                    f"Document GPS is {dist_km:.1f} km from project area "
                    f"(max allowed: {MAX_DOC_DIST_KM} km)."
                ),
            }

    # ── Layer B: authenticity ─────────────────────────────────────────────────
    auth = check_document_authenticity(file_bytes, filename, text, owner, doc_gps, project_id)

    # HARD FAILS — these block minting entirely:
    #   • Reuse: same deed cannot back two projects
    #   • Forgery: a deed produced by reportlab/Word/ChatGPT/Canva is never genuine
    if auth["is_reused"]:
        return {
            "hard_fail":  True, "score": 0, "owner_name": owner, "doc_gps": doc_gps,
            "gps_match":  gps_match, "confidence": 0.0,
            "authenticity": auth["authenticity"], "auth_flags": auth["flags"],
            "reason":     "Document reuse detected — " + auth["flags"][0],
        }
    if auth["is_forgery"]:
        forge_flag = next((f for f in auth["flags"] if f.startswith("FORGED")), "FORGED_DOCUMENT")
        return {
            "hard_fail":  True, "score": 0, "owner_name": owner, "doc_gps": doc_gps,
            "gps_match":  gps_match, "confidence": 0.0,
            "authenticity": auth["authenticity"], "auth_flags": auth["flags"],
            "reason":     "Forged document — " + forge_flag,
        }

    confidence = _doc_confidence(owner, doc_gps, gps_match, text)
    score      = int(25 * confidence * auth["authenticity"])

    parts = []
    if owner and owner != "Unknown":
        parts.append(f"Owner: {owner}")
    else:
        parts.append("Owner name not found.")
    if doc_gps:
        parts.append(f"GPS {doc_gps[0]:.4f},{doc_gps[1]:.4f}" + (f" matches ({dist_km:.1f}km)" if gps_match else ""))
    parts.append(f"Authenticity {int(auth['authenticity']*100)}%")
    if auth["is_forgery"]:
        parts.append("FORGERY SUSPECTED")

    return {
        "hard_fail":    False,
        "score":        score,
        "owner_name":   owner,
        "doc_gps":      doc_gps,
        "gps_match":    gps_match,
        "distance_km":  dist_km,
        "confidence":   round(confidence, 2),
        "authenticity": auth["authenticity"],
        "is_forgery":   auth["is_forgery"],
        "doc_hash":     auth["doc_hash"],
        "auth_flags":   auth["flags"],
        "reason":       ". ".join(parts),
    }

def _extract_text(file_bytes: bytes, filename: str) -> str:
    # extract plain text from a PDF or image file

    fname = (filename or "").lower()

    if fname.endswith(".pdf"):
        try:
            doc  = fitz.open(stream=file_bytes, filetype="pdf")
            text = " ".join(page.get_text() for page in doc)
            doc.close()
            return text
        except Exception as exc:
            log.warning("PDF extraction failed: %s", exc)
            return ""

    try:
        image = Image.open(io.BytesIO(file_bytes))
        return pytesseract.image_to_string(image)
    except Exception as exc:
        log.warning("OCR extraction failed: %s", exc)
        return ""

def _extract_owner(text: str) -> str:
    # extract the land owner name using spaCy NER or regex fallback

    if SPACY_AVAILABLE and _nlp:
        doc     = _nlp(text[:5_000])
        persons = [ent.text for ent in doc.ents if ent.label_ == "PERSON"]
        orgs    = [ent.text for ent in doc.ents if ent.label_ == "ORG"]

        patterns = [
            r"(?:owner|grantor|titleholder)[:\s]+([A-Z][a-z]+ [A-Z][a-z]+)",
            r"(?:Mr\.|Mrs\.|Ms\.)\s+([A-Z][a-z]+ [A-Z][a-z]+)",
        ]
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()

        if persons:
            return persons[0]
        if orgs:
            return orgs[0]

    match = re.search(
        r"(?:owner|grantor)[:\s]+([A-Z][a-zA-Z\s]{3,40})",
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip() if match else "Unknown"

def _extract_gps_from_text(text: str) -> Optional[List[float]]:
    # try each GPS regex pattern and return [lat, lon] on first match

    for pattern in GPS_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        groups = match.groups()
        try:
            if len(groups) == 2:
                lat, lon = float(groups[0]), float(groups[1])
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return [lat, lon]

            elif len(groups) == 8:
                lat = _dms_to_decimal(groups[0], groups[1], groups[2], groups[3])
                lon = _dms_to_decimal(groups[4], groups[5], groups[6], groups[7])
                return [lat, lon]
        except (ValueError, TypeError):
            continue
    return None

def _dms_to_decimal(deg: str, mins: str, secs: str, direction: str) -> float:
    # convert degrees-minutes-seconds + hemisphere to decimal degrees

    value = int(deg) + int(mins) / 60 + int(secs) / 3600
    return -value if direction.upper() in ("S", "W") else value

def _polygon_centroid(polygon: List[List[float]]) -> Optional[List[float]]:
    # return the arithmetic centroid [lat, lon] of a polygon

    if not polygon:
        return None
    return [
        sum(p[0] for p in polygon) / len(polygon),
        sum(p[1] for p in polygon) / len(polygon),
    ]

def _haversine(point_a: List[float], point_b: List[float]) -> float:
    # return great-circle distance in kilometres between two [lat, lon] points

    R    = 6_371
    lat1 = math.radians(point_a[0])
    lon1 = math.radians(point_a[1])
    lat2 = math.radians(point_b[0])
    lon2 = math.radians(point_b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a    = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))

def _doc_confidence(
    owner: str,
    gps:   Optional[List[float]],
    match: bool,
    text:  str,
) -> float:
    """Compute a 0-1 confidence score for the ownership document."""
    score = 0.0
    if owner and owner != "Unknown":
        score += 0.4
    if gps:
        score += 0.3
    if match:
        score += 0.3
    keywords = ["title", "deed", "cadastral", "land registry", "parcel"]
    if any(kw in text.lower() for kw in keywords):
        score = min(score + 0.1, 1.0)
    return score

# module 3 — anomaly detector (multi-layer)
# One IsolationForest has exactly one decision boundary, and anyone who studies
# the training data can park their numbers just inside the safe zone and farm the
# 25 points every time. So we stack five layers and make a claim clear all of them:
#   1. IPCC physical hard cap — biome maxima from the science. No ML to game.
#   2. Ensemble of 5 forests (different seeds/contamination), majority vote >=3/5.
#      Reverse-engineering one model's edge still leaves four standing.
#   3. Per-biome z-score — even past the ensemble, >2.5σ over the biome mean bleeds
#      points. This is what catches the "just inside the boundary" trick.
#   4. Boundary-proximity penalty — a claim sitting suspiciously close to a model's
#      threshold is too well-calibrated; real projects don't land on the line.
#   5. Cross-feature sanity — type/biome combos that don't make sense, e.g. a
#      renewable-energy project in a wetland.

# layer 1 constants: ipcc physical plausibility ceilings
# Source: IPCC AR6 WGIII, Table 7.1 — above these values is physically
# impossible for the given biome regardless of what the ML model says.
BIOME_HARD_CAPS: Dict[str, float] = {
    "tropical":  400.0,   # tCO2/ha
    "temperate": 250.0,
    "boreal":    180.0,
    "wetland":   700.0,
    "other":     350.0,
}

# layer 2 constants: ensemble configs
# Varying seeds + contamination. Features are log-transformed + StandardScaled
# before fitting, so scale differences between tph and categorical features
# don't skew the isolation trees.
ENSEMBLE_CONFIGS: List[Dict] = [
    {"n_estimators": 200, "contamination": 0.01, "random_state": 42,  "n_jobs": -1},
    {"n_estimators": 200, "contamination": 0.02, "random_state": 17,  "n_jobs": -1},
    {"n_estimators": 200, "contamination": 0.015,"random_state": 99,  "n_jobs": -1},
    {"n_estimators": 150, "contamination": 0.02, "random_state": 31,  "n_jobs": -1},
    {"n_estimators": 150, "contamination": 0.01, "random_state": 73,  "n_jobs": -1},
]
ENSEMBLE_MAJORITY = 3   # need ≥3/5 models to vote "normal"

# layer 4 constant
# IsolationForest.decision_function() near 0 = boundary. Legitimate projects
# should be comfortably inside normal territory (large positive value).
BOUNDARY_ZONE = 0.04   # tighter — only truly edge-hugging claims trigger this

# layer 5: type/biome combinations that make no physical sense
IMPLAUSIBLE_COMBOS: List[Tuple[str, str]] = [
    ("renewable", "tropical"),    # solar/wind farm inside a tropical forest?
    ("renewable", "boreal"),
    ("methane",   "boreal"),
    ("methane",   "temperate"),
]

def _encode_project_type(project_type: str) -> int:
    lower = project_type.lower()
    for idx, t in enumerate(PROJECT_TYPES):
        if t in lower:
            return idx
    return 4

def _encode_biome(biome: str) -> int:
    lower = biome.lower()
    for idx, b in enumerate(BIOMES):
        if b in lower:
            return idx
    return 4

def _build_training_features() -> Tuple[np.ndarray, Dict[str, Dict]]:
    # build training features and per-biome stats

    raw_rows: List[List[float]] = []   # raw tph — used for biome stats
    feat_rows: List[List[float]] = []  # log1p(tph) — fed to IsolationForest

    if DATA_PATH.exists():
        try:
            df = pd.read_csv(DATA_PATH)
            for _, row in df.iterrows():
                try:
                    tonnes  = float(str(row.get("Total VCUs Issued", 0)).replace(",", ""))
                    ha      = max(float(str(row.get("Project Area (ha)", 1)).replace(",", "")), 1)
                    tph     = tonnes / ha
                    bi      = float(_encode_biome(str(row.get("Biome", "other"))))
                    vintage = (int(str(row.get("Vintage Start", 2015))[:4]) - 2000) / 25
                    type_e  = float(_encode_project_type(str(row.get("Project Type", "other"))))
                    raw_rows.append([tph, bi])
                    feat_rows.append([math.log1p(tph), type_e, vintage, bi])
                except (ValueError, TypeError):
                    continue
        except Exception as exc:
            log.warning("Verra CSV failed: %s", exc)

    if len(feat_rows) < 50:
        np.random.seed(42)

        # Literature-grounded prior: each sample's tCO2/ha is drawn around the
        # median for its (project_type × biome) combination, so the 4-feature
        # space [log1p(tph), type, vintage, biome] is genuinely structured — the
        # ensemble learns that e.g. renewable+tropical ≈ 4 tCO2/ha while
        # forest+tropical ≈ 30, instead of treating tph as biome-only.
        # A real registry CSV (data/verra_projects.csv) overrides all of this.
        for _ in range(1600):
            bi     = np.random.randint(0, 5)
            ti     = np.random.randint(0, 5)
            bname  = BIOMES[bi]
            tname  = PROJECT_TYPES[ti]
            median = TYPE_BIOME_MEDIANS.get((tname, bname), BIOME_MEDIANS[bname])
            cap    = BIOME_HARD_CAPS[bname]

            if np.random.random() < 0.30:
                # Young / small / early-crediting project — lower yield
                tph = float(np.clip(
                    np.random.lognormal(np.log(max(median * 0.4, 0.6)), 0.5),
                    0.6, median * 0.9
                ))
            else:
                # Mature project at its type×biome median
                tph = float(np.clip(
                    np.random.lognormal(np.log(median), 0.55),
                    0.6, cap * 0.75
                ))

            vintage = np.random.uniform(0, 1)
            raw_rows.append([tph, float(bi)])
            feat_rows.append([math.log1p(tph), float(ti), vintage, float(bi)])

        # Fraud outliers — ONLY high-value outliers.
        # Ghost projects (near-zero tph) are NOT included here because mixing
        # near-zero and high-value outliers causes the ensemble to create a
        # "horseshoe" boundary that mis-flags legitimate low-range projects too.
        # Near-zero claims are caught by the GHOST_PROJECT explicit rule in Layer 5.
        fraud_high = [
            (390.0, 0), (340.0, 0), (310.0, 0),   # extreme tropical
            (220.0, 1), (190.0, 1),                # extreme temperate
            (130.0, 2), (110.0, 2),                # extreme boreal
            (560.0, 3), (500.0, 3),                # extreme wetland
        ]
        for tph, bi in fraud_high:
            raw_rows.append([tph, float(bi)])
            feat_rows.append([math.log1p(tph), 0.0, 0.5, float(bi)])

    raw_arr  = np.array(raw_rows)
    feat_arr = np.array(feat_rows)

    # Per-biome stats — computed in log-space to match the log-normal distribution.
    stats: Dict[str, Dict] = {}
    for bi, bname in enumerate(BIOMES):
        mask = raw_arr[:, 1] == float(bi)
        if mask.sum() > 5:
            col      = raw_arr[mask, 0]
            log_col  = np.log1p(col)
            stats[bname] = {
                "median":   float(np.median(col)),
                "log_mean": float(np.mean(log_col)),     # used for Layer 3 Z-score
                "log_std":  max(float(np.std(log_col)), 0.3),
            }
        else:
            med = BIOME_MEDIANS.get(bname, 20.0)
            stats[bname] = {"median": med, "log_mean": math.log1p(med), "log_std": 0.7}

    return feat_arr, stats

def _load_ensemble() -> None:
    # train or load the 5-model ensemble + StandardScaler (once on first request)

    global _ensemble, _ensemble_scaler, _ensemble_stats, _ensemble_ready
    if _ensemble_ready:
        return

    if MODEL_PATH.exists():
        try:
            with open(MODEL_PATH, "rb") as f:
                saved = pickle.load(f)
            if isinstance(saved, dict) and "ensemble" in saved and "scaler" in saved:
                _ensemble        = saved["ensemble"]
                _ensemble_scaler = saved["scaler"]
                _ensemble_stats  = saved["stats"]
                _ensemble_ready  = True
                log.info("Anomaly ensemble loaded (%d models).", len(_ensemble))
                return
        except Exception:
            pass   # stale format — retrain

    log.info("Training anomaly ensemble (5 models, log1p + StandardScaler)...")
    feats, stats = _build_training_features()

    # Fit scaler on training features so inference uses the same normalization
    scaler = StandardScaler()
    scaled = scaler.fit_transform(feats)

    models = []
    for cfg in ENSEMBLE_CONFIGS:
        m = IsolationForest(**cfg)
        m.fit(scaled)
        models.append(m)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"ensemble": models, "scaler": scaler, "stats": stats}, f)

    _ensemble        = models
    _ensemble_scaler = scaler
    _ensemble_stats  = stats
    _ensemble_ready  = True
    log.info("Ensemble trained on %d samples, %d models.", len(feats), len(models))

def check_anomaly(
    tonnes_co2:   float,
    hectares:     float,
    project_type: str,
    vintage_year: int,
    biome:        str,
) -> Dict[str, Any]:
    """
    Multi-layer anomaly detection. Returns score (0-25) and full audit trail
    of which layers fired, so judges can see exactly why something was flagged.
    """
    _load_ensemble()

    ha            = max(hectares, 1.0)
    tph           = tonnes_co2 / ha
    biome_key     = BIOMES[_encode_biome(biome)]
    type_key      = PROJECT_TYPES[_encode_project_type(project_type)]
    lo, hi        = BIOME_RANGES[biome_key]
    hard_cap      = BIOME_HARD_CAPS[biome_key]
    layer_flags:  List[str] = []
    deductions    = 0

    # ── Layer 1: IPCC Physical Hard Cap ───────────────────────────────────────
    if tph > hard_cap:
        return {
            "score":              0,
            "anomaly_score":      1.0,
            "is_outlier":         True,
            "tonnes_per_ha":      round(tph, 2),
            "expected_range":     f"{lo}-{hi} tCO2/ha for {biome_key}",
            "hard_cap":           hard_cap,
            "layers_triggered":   ["IPCC_HARD_CAP"],
            "reason": (
                f"HARD FAIL: {tph:.1f} tCO2/ha exceeds IPCC physical maximum "
                f"({hard_cap} tCO2/ha) for {biome_key} ecosystems. "
                f"Physically impossible claim."
            ),
        }

    # ── Layer 2: Ensemble Vote ────────────────────────────────────────────────
    # log1p(tph) + scale to match training distribution
    raw_feat = np.array([[math.log1p(tph), float(_encode_project_type(project_type)),
                          (vintage_year - 2000) / 25.0, float(_encode_biome(biome))]])
    features = _ensemble_scaler.transform(raw_feat)
    votes_normal = 0
    raw_scores   = []

    for model in _ensemble:
        pred = model.predict(features)[0]
        raw  = model.decision_function(features)[0]
        raw_scores.append(raw)
        if pred == 1:
            votes_normal += 1

    # Within-range leniency: if tph sits inside the biome's defined valid range
    # AND the log Z-score is not extreme, require only 2/5 ensemble agreement.
    # This prevents the ensemble from penalising the valid low-tail (e.g. 4 tCO2/ha
    # in tropical, which is rare but real). Outside the range: strict 3/5 majority.
    bstats_early = _ensemble_stats.get(biome_key, {"log_mean": math.log1p(BIOME_MEDIANS.get(biome_key, 20)), "log_std": 0.7})
    z_early      = (math.log1p(tph) - bstats_early.get("log_mean", 3.0)) / max(bstats_early.get("log_std", 0.7), 0.3)
    within_range  = (lo <= tph <= hi) and (abs(z_early) < 2.0)
    majority_need = 2 if within_range else ENSEMBLE_MAJORITY
    ensemble_pass = votes_normal >= majority_need
    avg_raw       = float(np.mean(raw_scores))
    anomaly_score = float(max(0.0, min(1.0, 1.0 - (avg_raw + 0.5))))

    if not ensemble_pass:
        layer_flags.append(f"ENSEMBLE_REJECTED ({votes_normal}/{len(_ensemble)} votes normal)")
        deductions += 20

    # ── Layer 3: Log-space Z-score (biome-specific) ───────────────────────────
    # Compute Z-score on log1p(tph) because the distribution is log-normal.
    # Raw-space Z-scores are misleading: a value of 85 looks like +3.4σ in
    # raw space but is only +1.7σ in log space — the correct representation.
    bstats    = _ensemble_stats.get(biome_key, {"log_mean": math.log1p(BIOME_MEDIANS.get(biome_key, 20)),
                                                 "log_std":  0.7})
    log_mean  = bstats.get("log_mean", math.log1p(BIOME_MEDIANS.get(biome_key, 20)))
    log_std   = max(bstats.get("log_std", 0.7), 0.3)
    z_score   = (math.log1p(tph) - log_mean) / log_std

    # Asymmetric thresholds: fraud is almost always HIGH claims, not low ones.
    # Low claims (young/degraded projects) are penalised less severely.
    if z_score > 2.0:
        layer_flags.append(f"Z_SCORE_HIGH ({z_score:.1f}σ above {biome_key} log-mean)")
        deductions += 8 if z_score > 3.0 else 4
    elif z_score < -2.5:
        layer_flags.append(f"Z_SCORE_LOW ({z_score:.1f}σ below {biome_key} log-mean)")
        deductions += 4

    # ── Layer 4: Ghost project — near-zero tCO2/ha ───────────────────────────
    # Checked separately: mixing near-zero fraud into ensemble training data
    # creates a horseshoe boundary that mis-flags legitimate low-range projects.
    if tph < 0.5:
        layer_flags.append(
            f"GHOST_PROJECT: {tph:.3f} tCO2/ha is below any real project minimum — "
            f"likely inflated hectares or zero-sequestration land."
        )
        deductions += 15

    # ── Layer 5: Cross-feature Consistency ────────────────────────────────────
    for bad_type, bad_biome in IMPLAUSIBLE_COMBOS:
        if bad_type in type_key and bad_biome in biome_key:
            layer_flags.append(
                f"TYPE_BIOME_MISMATCH: {project_type} project inside {biome} ecosystem"
            )
            deductions += 4
            break

    # Vintage sanity: claiming credits for future years — always fraud
    if vintage_year > datetime.utcnow().year:
        layer_flags.append(f"FUTURE_VINTAGE: vintage {vintage_year} has not occurred yet")
        deductions += 20   # guaranteed outlier regardless of ensemble vote

    # Very round numbers on large claims are a social engineering signal
    if tonnes_co2 >= 10_000 and tonnes_co2 % 1_000 == 0 and hectares % 100 == 0:
        layer_flags.append("SUSPICIOUSLY_ROUND_CLAIM: exact round numbers on large project")
        deductions += 2

    # ── Layer 6: Real Verra volume realism (1,400+ real projects) ─────────────
    # Hectare-independent sanity check on total claim magnitude vs. what real
    # registered projects of this type actually produce. Deliberately conservative
    # (>3σ in log space) so it only catches absurd volume claims, never legit ones.
    vstats = _VERRA_VOL_STATS.get(type_key)
    if vstats and tonnes_co2 > 0:
        z_vol = (math.log(tonnes_co2) - vstats["log_mean"]) / vstats["log_std"]
        if z_vol > 3.0:
            layer_flags.append(
                f"VOLUME_OUTLIER_VS_VERRA: {tonnes_co2:,.0f} tCO2 is {z_vol:.1f}σ above "
                f"real {type_key} projects (Verra median {vstats['median']:,.0f}/yr)"
            )
            deductions += 10

    # ── Final score ───────────────────────────────────────────────────────────
    base_score = 25 if ensemble_pass else 5
    score      = max(0, base_score - deductions)
    is_outlier = not ensemble_pass or deductions >= 10

    # Build a human-readable reason for the audit report
    if not layer_flags:
        reason = (
            f"Claim of {tph:.1f} tCO2/ha passed all 5 detection layers. "
            f"Within {biome_key} {project_type} norms ({lo}-{hi} tCO2/ha). "
            f"Ensemble: {votes_normal}/{len(_ensemble)} models normal. "
            f"Z-score: {z_score:.2f}."
        )
    else:
        reason = (
            f"Claim of {tph:.1f} tCO2/ha triggered {len(layer_flags)} anomaly layer(s). "
            f"Ensemble: {votes_normal}/{len(_ensemble)} models normal. "
            f"Z-score: {z_score:.2f}. Flags: {' | '.join(layer_flags)}."
        )

    return {
        "score":              score,
        "anomaly_score":      round(anomaly_score, 3),
        "is_outlier":         is_outlier,
        "tonnes_per_ha":      round(tph, 2),
        "expected_range":     f"{lo}-{hi} tCO2/ha for {biome_key}",
        "hard_cap":           hard_cap,
        "ensemble_votes":     f"{votes_normal}/{len(_ensemble)}",
        "z_score":            round(z_score, 2),
        "layers_triggered":   layer_flags,
        "reason":             reason,
    }

# module 4 — nasa firms satellite check
def _fetch_firms_chunk(api_key: str, bbox: str, start_date: str) -> int:
    # fetch one 5-day FIRMS window. Returns the fire-alert count for that window,

    url = f"{NASA_BASE_URL}/{api_key}/{NASA_SOURCE}/{bbox}/{NASA_CHUNK_DAYS}/{start_date}"
    try:
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        raw = resp.text.strip()
        if not raw or raw.startswith("Invalid") or raw.startswith("You have"):
            return -1
        rows = list(csv.reader(io.StringIO(raw)))
        return max(len(rows) - 1, 0)   # minus header
    except Exception as exc:
        log.warning("FIRMS chunk %s failed: %s", start_date, str(exc)[:60])
        return -1

def check_nasa_firms(polygon: List[List[float]]) -> Dict[str, Any]:
    # query real NASA FIRMS satellite data for fire / deforestation alerts in the

    api_key = os.getenv("NASA_FIRMS_API_KEY")
    lats    = [p[0] for p in polygon]
    lons    = [p[1] for p in polygon]

    lat_mid  = sum(lats) / len(lats)
    area_km2 = max(
        (max(lats) - min(lats)) * 111
        * (max(lons) - min(lons)) * 111
        * math.cos(math.radians(lat_mid)),
        0.01,
    )

    if not api_key:
        log.warning("NASA_FIRMS_API_KEY not set — returning mock data.")
        return {
            "score":           20,
            "fire_count":      0,
            "alert_density":   0.0,
            "area_km2":        round(area_km2, 2),
            "permanence_risk": "low",
            "reason":          "Mock data — set NASA_FIRMS_API_KEY for real satellite checks.",
            "is_mock":         True,
        }

    # FIRMS area format: west,south,east,north
    bbox = f"{min(lons)},{min(lats)},{max(lons)},{max(lats)}"

    # Build 5-day window start dates covering the last 90 days
    today        = datetime.utcnow().date()
    n_chunks     = NASA_DAYS // NASA_CHUNK_DAYS          # 90 / 5 = 18
    start_dates  = [
        (today - timedelta(days=NASA_CHUNK_DAYS * (i + 1))).isoformat()
        for i in range(n_chunks)
    ]

    # Fetch all windows concurrently (FIRMS allows 5000 tx / 10 min)
    total_fires = 0
    failed      = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_firms_chunk, api_key, bbox, d): d for d in start_dates}
        for fut in as_completed(futures):
            count = fut.result()
            if count < 0:
                failed += 1
            else:
                total_fires += count

    # If every single window failed, the API is unreachable — be honest
    if failed == n_chunks:
        log.error("All %d NASA FIRMS windows failed.", n_chunks)
        return {
            "score":           15,
            "fire_count":      0,
            "alert_density":   0.0,
            "area_km2":        round(area_km2, 2),
            "permanence_risk": "unknown",
            "reason":          "NASA FIRMS API unreachable — could not verify satellite data.",
        }

    days_covered = (n_chunks - failed) * NASA_CHUNK_DAYS
    density      = round(total_fires / area_km2, 4)

    if total_fires == 0:
        return {
            "score": 20, "fire_count": 0, "alert_density": 0.0,
            "area_km2": round(area_km2, 2), "permanence_risk": "low",
            "reason": f"No fire alerts in {days_covered} days of real VIIRS satellite data.",
        }
    elif total_fires < 3:
        return {
            "score": 16, "fire_count": total_fires, "alert_density": density,
            "area_km2": round(area_km2, 2), "permanence_risk": "low",
            "reason": f"{total_fires} minor fire alert(s) in {days_covered} days — low permanence risk.",
        }
    elif total_fires < 10:
        return {
            "score": 8, "fire_count": total_fires, "alert_density": density,
            "area_km2": round(area_km2, 2), "permanence_risk": "medium",
            "reason": f"{total_fires} fire alerts in {days_covered} days — medium permanence risk.",
        }
    else:
        return {
            "score": 0, "fire_count": total_fires, "alert_density": density,
            "area_km2": round(area_km2, 2), "permanence_risk": "high",
            "reason": f"{total_fires} fire alerts in {days_covered} days — HIGH permanence risk. Forest likely degraded.",
        }

# module 5 — audit report (llama 3 / ollama)
def generate_audit_report(data: Dict[str, Any]) -> str:
    # use Llama 3 (local Ollama) to write a professional carbon audit report

    verdict  = "APPROVED FOR MINTING" if data["verdict"] == "PASS" else "REJECTED"
    flags_str = ", ".join(data["flags"]) if data["flags"] else "None"

    prompt = f"""You are a professional carbon credit auditor. Write a formal verification report.
Be concise, professional, and factual. Plain English only — no markdown. Maximum 400 words.

Project: {data['project_name']} ({data['project_id']})
Date: {datetime.utcnow().strftime('%Y-%m-%d')}
Verdict: {verdict}
Total Score: {data['total_score']}/100

Module Scores:
  GPS duplicate check : {data['scores'].get('gps', 0)}/30  — {data['gps_result'].get('reason', '')}
  Ownership document  : {data['scores'].get('ownership', 0)}/25 — {data['doc_result'].get('reason', '')}
  Anomaly detection   : {data['scores'].get('anomaly', 0)}/25  — {data['anomaly_result'].get('reason', '')}
  Satellite check     : {data['scores'].get('satellite', 0)}/20 — {data['satellite_result'].get('reason', '')}

Carbon Claim: {data['tonnes_co2']} tCO₂ / {data['hectares']} ha
  = {data['anomaly_result'].get('tonnes_per_ha', 'N/A')} tCO₂/ha
Expected range: {data['anomaly_result'].get('expected_range', 'N/A')}
Fire alerts (90 days): {data['satellite_result'].get('fire_count', 0)}
Flags: {flags_str}

Write: executive summary, per-module findings, and a recommendation."""

    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model":   OLLAMA_MODEL,
                "prompt":  prompt,
                "stream":  False,
                "options": {"temperature": 0.3, "num_predict": 450},
            },
            timeout=90,   # local LLM on M1 — first call loads the model
        )
        resp.raise_for_status()
        report = resp.json().get("response", "").strip()
        if report:
            return report
    except requests.exceptions.ConnectionError:
        log.warning("Ollama not running — using template. Start: ollama serve")
    except Exception as exc:
        log.error("Ollama error: %s", exc)

    return _fallback_report(data)

def generate_audit_pdf(report_text: str, project_id: str) -> Optional[Path]:
    # render audit report text to a PDF file using ReportLab

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

        PDF_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = PDF_DIR / f"{project_id[:16]}.pdf"

        doc    = SimpleDocTemplate(
            str(pdf_path), pagesize=A4,
            leftMargin=2 * cm, rightMargin=2 * cm,
            topMargin=2 * cm, bottomMargin=2 * cm,
        )
        styles = getSampleStyleSheet()
        story  = []

        for line in report_text.split("\n"):
            if line.strip():
                safe_line = line.replace("&", "&amp;").replace("<", "&lt;")
                story.append(Paragraph(safe_line, styles["Normal"]))
            else:
                story.append(Spacer(1, 0.3 * cm))

        doc.build(story)
        log.info("PDF report saved: %s", pdf_path)
        return pdf_path

    except ImportError:
        log.warning("reportlab not installed — skipping PDF generation.")
    except Exception as exc:
        log.error("PDF generation failed: %s", exc)

    return None

def _fallback_report(data: Dict[str, Any]) -> str:
    # deterministic report template — used when Ollama is unavailable

    verdict   = "APPROVED FOR MINTING" if data["verdict"] == "PASS" else "REJECTED"
    flags_str = ", ".join(data["flags"]) if data["flags"] else "None"
    scores    = data["scores"]

    return (
        f"TERRALEDGER VERIFICATION REPORT\n"
        f"{'=' * 48}\n"
        f"Date    : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Project : {data['project_name']} ({data['project_id']})\n"
        f"Verdict : {verdict}  |  Score: {data['total_score']}/100\n\n"
        f"FINDINGS\n"
        f"{'-' * 48}\n"
        f"GPS duplicate  ({scores.get('gps', 0)}/30): "
        f"{data['gps_result'].get('reason', 'N/A')}\n"
        f"Ownership doc  ({scores.get('ownership', 0)}/25): "
        f"{data['doc_result'].get('reason', 'N/A')}\n"
        f"Anomaly AI     ({scores.get('anomaly', 0)}/25): "
        f"{data['anomaly_result'].get('reason', 'N/A')}\n"
        f"Satellite      ({scores.get('satellite', 0)}/20): "
        f"{data['satellite_result'].get('reason', 'N/A')}\n\n"
        f"Carbon claim   : {data['tonnes_co2']} tCO₂ / {data['hectares']} ha"
        f" = {data['anomaly_result'].get('tonnes_per_ha', 'N/A')} tCO₂/ha\n"
        f"Expected range : {data['anomaly_result'].get('expected_range', 'N/A')}\n"
        f"Fire alerts    : {data['satellite_result'].get('fire_count', 0)} (90-day window)\n"
        f"Flags          : {flags_str}\n\n"
        f"RECOMMENDATION\n"
        f"{'-' * 48}\n"
        + (
            "Carbon credit NFT may be minted on QIE blockchain. "
            "This audit report is stored on IPFS and linked permanently to the NFT metadata."
            if data["verdict"] == "PASS"
            else
            "Submission rejected. The rejection is permanently logged on-chain as a deterrent."
        )
        + f"\n\n─ TerraLedger · QIE Blockchain · {datetime.utcnow().year} ─"
    )

# ipfs uploader (pinata)
PINATA_PIN_JSON_URL = "https://api.pinata.cloud/pinning/pinJSONToIPFS"

def upload_to_ipfs(
    report_text: str,
    project_id:  str,
    scores:      Dict[str, int],
    verdict:     str,
    total_score: int,
) -> Optional[str]:
    """
    Pin the AI audit report to IPFS via Pinata. Returns the real IPFS CID.

    The CID is stored on-chain in the NFT metadata and linked in the audit,
    so any buyer can independently fetch the full audit report from IPFS
    forever via https://gateway.pinata.cloud/ipfs/<cid> (or any IPFS gateway).

    Returns None if Pinata is not configured or the upload fails — the caller
    treats a missing CID as "audit not yet pinned" rather than faking one.
    """
    jwt = os.getenv("PINATA_JWT")

    if not jwt:
        log.warning("PINATA_JWT not set — audit report will NOT be pinned to IPFS.")
        return None

    body = {
        "pinataMetadata": {
            "name": f"terraledger-audit-{project_id}",
            "keyvalues": {
                "project_id":  project_id,
                "verdict":     verdict,
                "total_score": str(total_score),
            },
        },
        "pinataContent": {
            "project_id":   project_id,
            "audit_report": report_text,
            "scores":       scores,
            "verdict":      verdict,
            "total_score":  total_score,
            "timestamp":    datetime.utcnow().isoformat(),
            "issuer":       "TerraLedger AI Verification · QIE Blockchain",
        },
    }

    try:
        resp = requests.post(
            PINATA_PIN_JSON_URL,
            headers={
                "Authorization": f"Bearer {jwt}",
                "Content-Type":  "application/json",
            },
            json=body,
            timeout=20,
        )
        resp.raise_for_status()
        cid = resp.json().get("IpfsHash")
        if cid:
            log.info("IPFS pinned via Pinata: %s", cid)
        return cid
    except Exception as exc:
        log.error("Pinata IPFS upload failed: %s", exc)
        return None

# qie pass rest client (real identity verification)
# Worth being clear: real QIE Pass is a REST API, not an on-chain isVerified().
# The on-chain MockQIEPass is just the on-chain mirror. The actual partner flow is
# three calls — create a verification request, poll until the user consents, then
# claim the signed claims — authed with HMAC-SHA256 over (publicKey + timestamp).
# Keys come from env (QIE_PASS_PUBLIC_KEY / _SECRET_KEY / _BASE_URL); with no keys
# the client reports "not configured" and we fall back to the MockQIEPass path.

QIE_PASS_BASE_URL = os.getenv("QIE_PASS_BASE_URL", "https://pass-api.qie.digital").rstrip("/")

class QIEPassClient:
    """Partner-side client for QIE Pass privacy-preserving identity verification."""

    def __init__(self) -> None:
        self.public_key = os.getenv("QIE_PASS_PUBLIC_KEY", "").strip()
        self.secret_key = os.getenv("QIE_PASS_SECRET_KEY", "").strip()
        self.base_url   = QIE_PASS_BASE_URL
        self.ready      = bool(self.public_key and self.secret_key)
        if self.ready:
            log.info("QIE Pass client ready | key=%s… base=%s", self.public_key[:10], self.base_url)
        else:
            log.warning("QIE Pass API keys not set — falling back to on-chain MockQIEPass demo mode.")

    def _headers(self) -> Dict[str, str]:
        # hMAC-SHA256 signed headers. Signature = HMAC(secret, publicKey + timestamp)

        timestamp = str(int(time.time() * 1000))          # unix ms
        message   = self.public_key + timestamp
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Public-Key": self.public_key,
            "X-Signature":  signature,
            "X-Timestamp":  timestamp,
        }

    def create_verification_request(self, identifier: str, claims: List[str]) -> Dict[str, Any]:
        # start an identity verification for a user (DID or wallet address)

        if not self.ready:
            raise HTTPException(status_code=503, detail="QIE Pass API not configured (set QIE_PASS_PUBLIC_KEY / QIE_PASS_SECRET_KEY).")
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/partners/verification-requests",
                headers=self._headers(),
                json={"identifier": identifier, "requestedClaims": claims},
                timeout=15,
            )
            return self._unwrap(resp)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"QIE Pass unreachable: {str(exc)[:120]}")

    def get_request_status(self, request_id: str) -> Dict[str, Any]:
        # poll a verification request. Status flow: pending_kyc → pending_consent → consent_given / consent_rejected

        if not self.ready:
            raise HTTPException(status_code=503, detail="QIE Pass API not configured.")
        try:
            resp = requests.get(
                f"{self.base_url}/api/v1/partners/verification-requests/{request_id}",
                headers=self._headers(),
                timeout=15,
            )
            return self._unwrap(resp)
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"QIE Pass unreachable: {str(exc)[:120]}")

    def claim_and_verify(self, request_id: str) -> Dict[str, Any]:
        # claim the verified credential once consent is given. Returns selective-

        if not self.ready:
            raise HTTPException(status_code=503, detail="QIE Pass API not configured.")
        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/vc/partner/claim-and-verify",
                headers=self._headers(),
                json={"requestId": request_id},
                timeout=15,
            )
            data = self._unwrap(resp)
            v = data.get("verification", {})
            data["fully_valid"] = bool(
                v.get("signatureValid") and v.get("notExpired") and v.get("notRevoked")
            )
            return data
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"QIE Pass unreachable: {str(exc)[:120]}")

    @staticmethod
    def _unwrap(resp: "requests.Response") -> Dict[str, Any]:
        # qIE Pass wraps payloads as {success, data|...}. Surface errors as HTTP errors

        try:
            body = resp.json()
        except Exception:
            raise HTTPException(status_code=502, detail=f"QIE Pass returned non-JSON ({resp.status_code}).")
        if resp.status_code >= 400 or body.get("success") is False:
            err = (body.get("error") or {}).get("message") or body.get("message") or f"HTTP {resp.status_code}"
            raise HTTPException(status_code=resp.status_code if resp.status_code >= 400 else 400,
                                detail=f"QIE Pass: {err}")
        return body.get("data", body)

# Module-level singleton — initialised once at import
_qiepass = QIEPassClient()

# blockchain oracle client
_TX_LOCK = threading.Lock()   # serialize oracle-wallet tx sending (nonce safety)

class BlockchainOracle:
    """
    Signs and submits AI verification results to CarbonOracle.sol on QIE testnet.

    Requires:
        PRIVATE_KEY              — oracle signer wallet
        ORACLE_CONTRACT_ADDRESS  — deployed CarbonOracle address
        QIE_RPC_URL              — defaults to https://rpc1testnet.qie.digital/
    """

    # ABI for CarbonOracle.submitVerification
    ORACLE_ABI = [
        {
            "inputs": [
                {"name": "projectId",         "type": "bytes32"},
                {"name": "gpsScore",          "type": "uint256"},
                {"name": "ownershipScore",    "type": "uint256"},
                {"name": "anomalyScore",      "type": "uint256"},
                {"name": "satelliteScore",    "type": "uint256"},
                {"name": "gpsHardFail",       "type": "bool"},
                {"name": "ownershipHardFail", "type": "bool"},
                {"name": "reportIpfsCid",     "type": "string"},
                {"name": "flags",             "type": "string[]"},
                {"name": "vintage",           "type": "uint256"},
                {"name": "tonnes",            "type": "uint256"},
                {"name": "docHash",           "type": "bytes32"},
            ],
            "name": "submitVerification",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
        {
            "inputs": [
                {"name": "projectId", "type": "bytes32"},
                {"name": "requester", "type": "address"},
            ],
            "name": "hasDocumentAccess",
            "outputs": [{"type": "bool"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [{"name": "projectId", "type": "bytes32"}],
            "name": "finalized",
            "outputs": [{"type": "bool"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [], "name": "attestationThreshold",
            "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function",
        },
        {
            "inputs": [], "name": "oracleCount",
            "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function",
        },
    ]

    # ABI for ProjectRegistry.submitProject + getProject
    REGISTRY_ABI = [
        {
            "inputs": [
                {"name": "id",             "type": "bytes32"},
                {"name": "polygonGeoJSON", "type": "string"},
                {"name": "areaHectares",   "type": "uint256"},
                {"name": "claimedTonnes",  "type": "uint256"},
                {"name": "minLat",         "type": "int64"},
                {"name": "minLng",         "type": "int64"},
                {"name": "maxLat",         "type": "int64"},
                {"name": "maxLng",         "type": "int64"},
            ],
            "name": "submitProject",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        },
        {
            "inputs": [{"name": "id", "type": "bytes32"}],
            "name": "getProject",
            "outputs": [{"components": [
                {"name": "id", "type": "bytes32"}, {"name": "owner", "type": "address"},
                {"name": "polygonGeoJSON", "type": "string"}, {"name": "areaHectares", "type": "uint256"},
                {"name": "claimedTonnes", "type": "uint256"}, {"name": "submittedAt", "type": "uint256"},
                {"name": "status", "type": "uint8"}, {"name": "statusReason", "type": "string"},
                {"name": "minLat", "type": "int64"}, {"name": "minLng", "type": "int64"},
                {"name": "maxLat", "type": "int64"}, {"name": "maxLng", "type": "int64"},
            ], "type": "tuple"}],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [
                {"name": "minLat", "type": "int64"}, {"name": "minLng", "type": "int64"},
                {"name": "maxLat", "type": "int64"}, {"name": "maxLng", "type": "int64"},
            ],
            "name": "findOverlaps",
            "outputs": [{"type": "bytes32[]"}],
            "stateMutability": "view",
            "type": "function",
        },
    ]

    # ABI for CarbonCredit — read minted token id + transfer to project owner
    CREDIT_ABI = [
        {"inputs": [{"name": "projectId", "type": "bytes32"}], "name": "getProjectTokens",
         "outputs": [{"type": "uint256[]"}], "stateMutability": "view", "type": "function"},
        {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "ownerOf",
         "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
        {"inputs": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                    {"name": "tokenId", "type": "uint256"}], "name": "transferFrom",
         "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    ]

    def __init__(self) -> None:
        self._w3       = None
        self._contract = None     # CarbonOracle
        self._registry = None     # ProjectRegistry
        self._credit   = None     # CarbonCredit
        self._account  = None
        self._ready    = False
        self._setup()

    def _setup(self) -> None:
        if not WEB3_AVAILABLE:
            log.warning("web3 not installed — pip install web3. On-chain calls disabled.")
            return

        private_key   = os.getenv("PRIVATE_KEY")
        oracle_addr   = os.getenv("ORACLE_CONTRACT_ADDRESS")
        registry_addr = os.getenv("NEXT_PUBLIC_PROJECT_REGISTRY_ADDRESS")
        credit_addr   = os.getenv("NEXT_PUBLIC_CARBON_CREDIT_ADDRESS")
        rpc_url       = os.getenv("QIE_RPC_URL", "https://rpc1testnet.qie.digital/")

        if not private_key or not oracle_addr:
            log.warning(
                "BlockchainOracle disabled — "
                "set PRIVATE_KEY and ORACLE_CONTRACT_ADDRESS in .env"
            )
            return

        try:
            self._w3 = Web3(Web3.HTTPProvider(rpc_url))
            if not self._w3.is_connected():
                log.error("Cannot connect to QIE RPC at %s", rpc_url)
                return

            self._account  = EthAccount.from_key(private_key)
            self._contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(oracle_addr),
                abi=self.ORACLE_ABI,
            )
            if registry_addr:
                self._registry = self._w3.eth.contract(
                    address=Web3.to_checksum_address(registry_addr),
                    abi=self.REGISTRY_ABI,
                )
            if credit_addr:
                self._credit = self._w3.eth.contract(
                    address=Web3.to_checksum_address(credit_addr),
                    abi=self.CREDIT_ABI,
                )
            self._ready = True
            log.info("BlockchainOracle ready | signer=%s…", self._account.address[:10])
        except Exception as exc:
            log.error("BlockchainOracle init failed: %s", exc)

    def _send_tx(self, func_call, gas: int = 600_000) -> Optional[Any]:
        # sign and send a contract function call from the oracle wallet

        # Pre-flight: estimate gas — reverts here if the call would revert
        try:
            est = func_call.estimate_gas({"from": self._account.address})
            gas = int(est * 1.5)
        except Exception as exc:
            log.error("Gas estimate failed (call would revert): %s", str(exc)[:120])
            return None

        # Serialize nonce-read → send so concurrent submissions can't reuse a
        # nonce (single oracle wallet). "pending" nonce accounts for in-flight txs.
        with _TX_LOCK:
            nonce = self._w3.eth.get_transaction_count(self._account.address, "pending")
            tx    = func_call.build_transaction({
                "from":     self._account.address,
                "nonce":    nonce,
                "gas":      gas,
                "gasPrice": self._w3.eth.gas_price,
                "chainId":  self._w3.eth.chain_id,   # dynamic: 1983 testnet / 1990 mainnet
            })
            signed  = self._account.sign_transaction(tx)
            raw_tx  = getattr(signed, "rawTransaction", None) or signed.raw_transaction
            tx_hash = self._w3.eth.send_raw_transaction(raw_tx)
        return self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)

    @staticmethod
    def _hex(tx_hash) -> str:
        h = tx_hash.hex()
        return h if h.startswith("0x") else "0x" + h

    def submit(
        self,
        project_id:         str,
        gps_score:          int,
        ownership_score:    int,
        anomaly_score:      int,
        satellite_score:    int,
        gps_hard_fail:      bool,
        ownership_hard_fail: bool,
        report_cid:         str,
        flags:              List[str],
        vintage:            int,
        tonnes:             int,
        polygon:            Optional[List[List[float]]] = None,
        hectares:           int = 0,
        recipient:          Optional[str] = None,
        is_pass:            bool = False,
        doc_hash:           str = "",
    ) -> Optional[str]:
        """
        Full on-chain flow on QIE testnet:
          1. ProjectRegistry.submitProject()  — register project as Pending
          2. CarbonOracle.submitVerification() — approve+mint / reject / flag fraud
          3. (on PASS) transfer the minted NFT from the oracle wallet to the
             real project owner (recipient), so the credit lands in the user's
             wallet — not the oracle's. They can then list/retire it.

        Returns the submitVerification tx hash, or None.
        """
        if not self._ready:
            return None

        project_id_b32 = self._str_to_bytes32(project_id)

        # ── Step 1: Register in ProjectRegistry (if not already there) ─────────
        if self._registry is not None:
            try:
                existing = self._registry.functions.getProject(project_id_b32).call()
                already_registered = existing[5] != 0   # submittedAt != 0

                if not already_registered:
                    # Gas-min: store only the bbox on-chain (used for overlap) and a
                    # compact polygon ref, NOT the full GeoJSON coordinate string
                    # (string storage is the heaviest part of submitProject). The
                    # precise polygon lives in the IPFS audit; on-chain overlap uses
                    # the bbox below. Saves ~80–100k gas per submission.
                    geojson = "{}"
                    # Bounding box in microdegrees (lat/lng × 1e6) for the on-chain
                    # coarse overlap pre-filter (ProjectRegistry.findOverlaps).
                    if polygon:
                        lats = [p[0] for p in polygon]
                        lngs = [p[1] for p in polygon]
                        min_lat, max_lat = int(min(lats) * 1e6), int(max(lats) * 1e6)
                        min_lng, max_lng = int(min(lngs) * 1e6), int(max(lngs) * 1e6)
                    else:
                        min_lat = min_lng = max_lat = max_lng = 0
                    reg_receipt = self._send_tx(
                        self._registry.functions.submitProject(
                            project_id_b32, geojson, max(int(hectares), 1), max(int(tonnes), 1),
                            min_lat, min_lng, max_lat, max_lng,
                        ),
                        gas=900_000,   # string storage is gas-heavy
                    )
                    if reg_receipt is None or reg_receipt.status != 1:
                        log.error("submitProject reverted for %s", project_id)
                        return None
                    log.info("Registered %s on-chain (tx %s)", project_id, self._hex(reg_receipt.transactionHash)[:18])
            except Exception as exc:
                log.error("Registry step failed: %s", exc)
                return None

        # ── Step 2: Submit verification (oracle approves/rejects/flags) ────────
        # Convert the hex SHA-256 doc hash to bytes32 (zero hash if no document)
        doc_hash_b32 = bytes.fromhex(doc_hash[:64]) if doc_hash else b"\x00" * 32
        doc_hash_b32 = doc_hash_b32.ljust(32, b"\x00")
        try:
            receipt = self._send_tx(
                self._contract.functions.submitVerification(
                    project_id_b32,
                    gps_score, ownership_score, anomaly_score, satellite_score,
                    gps_hard_fail, ownership_hard_fail,
                    report_cid or "", flags, vintage, tonnes, doc_hash_b32,
                ),
                gas=700_000,
            )
            if receipt is None:
                return None
            hex_hash = self._hex(receipt.transactionHash)
            status   = "success" if receipt.status == 1 else "reverted"
            log.info("submitVerification tx %s — %s", hex_hash[:18], status)
            if receipt.status != 1:
                return None

        except Exception as exc:
            log.error("submitVerification failed: %s", exc)
            return None

        # ── Step 3: Transfer minted NFT oracle → real project owner ───────────
        # On a PASS the NFT was minted to the oracle (registrar). Hand it to the
        # actual submitter so it shows up in their wallet and they can list it.
        if is_pass and recipient and self._credit is not None:
            try:
                valid_recipient = (
                    Web3.is_address(recipient)
                    and int(recipient, 16) != 0
                    and recipient.lower() != self._account.address.lower()
                )
                if valid_recipient:
                    token_ids = self._credit.functions.getProjectTokens(project_id_b32).call()
                    if token_ids:
                        token_id = token_ids[-1]
                        owner    = self._credit.functions.ownerOf(token_id).call()
                        if owner.lower() == self._account.address.lower():
                            t_receipt = self._send_tx(
                                self._credit.functions.transferFrom(
                                    self._account.address,
                                    Web3.to_checksum_address(recipient),
                                    token_id,
                                ),
                                gas=200_000,
                            )
                            if t_receipt and t_receipt.status == 1:
                                log.info("NFT #%s transferred to project owner %s…",
                                         token_id, recipient[:10])
            except Exception as exc:
                log.warning("NFT transfer to owner failed (credit still minted): %s", exc)

        return hex_hash

    # QIE Pass on-chain bridge: after a REAL QIE Pass REST verification succeeds,
    # the oracle (which owns MockQIEPass) records the verified identity on-chain so
    # the contract gate (requestDocumentAccess → isVerified) reflects real KYC —
    # NOT public self-attestation. issueIdentity is onlyOwner.
    QIEPASS_WRITE_ABI = [
        {"inputs": [{"name": "wallet", "type": "address"}, {"name": "fullName", "type": "string"},
                    {"name": "organization", "type": "string"}],
         "name": "issueIdentity", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
        {"inputs": [{"name": "wallet", "type": "address"}], "name": "isVerified",
         "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
    ]

    def attest_identity(self, wallet: str, full_name: str, organization: str) -> Optional[str]:
        # record a real-QIE-Pass-verified wallet on-chain (oracle owns MockQIEPass)

        if not self._ready:
            return None
        qiepass_addr = os.getenv("NEXT_PUBLIC_QIEPASS_ADDRESS")
        if not qiepass_addr:
            return None
        try:
            if not Web3.is_address(wallet):
                return None
            c = self._w3.eth.contract(address=Web3.to_checksum_address(qiepass_addr), abi=self.QIEPASS_WRITE_ABI)
            if c.functions.isVerified(Web3.to_checksum_address(wallet)).call():
                return "already_verified"
            receipt = self._send_tx(
                c.functions.issueIdentity(Web3.to_checksum_address(wallet),
                                          full_name or "QIE Pass holder", organization or "QIE Pass verified"),
                gas=200_000,
            )
            if receipt and receipt.status == 1:
                return self._hex(receipt.transactionHash)
        except Exception as exc:
            log.warning("attest_identity failed: %s", str(exc)[:100])
        return None

    def has_document_access(self, project_id: str, wallet: str) -> Optional[bool]:
        # read the on-chain QIE Pass-gated document access grant for (project, wallet)

        if not self._ready or self._contract is None:
            return None
        try:
            pid = self._str_to_bytes32(project_id)
            return bool(self._contract.functions.hasDocumentAccess(
                pid, Web3.to_checksum_address(wallet)
            ).call())
        except Exception as exc:
            log.warning("hasDocumentAccess read failed: %s", str(exc)[:80])
            return None

    @staticmethod
    def _str_to_bytes32(s: str) -> bytes:
        # convert a project ID string to a bytes32 value

        encoded = s.encode("utf-8")
        if len(encoded) > 32:
            return hashlib.sha256(encoded).digest()
        return encoded.ljust(32, b"\x00")

# Module-level singleton — initialised once at import
_oracle = BlockchainOracle()

def _chain_id() -> int:
    # live chain id — ask the node first, fall back to env, then mainnet

    try:
        if _oracle._w3:
            return _oracle._w3.eth.chain_id
    except Exception:
        pass
    try:
        return int(os.getenv("NEXT_PUBLIC_QIE_CHAIN_ID", "1990"))
    except Exception:
        return 1990

def _network_label() -> str:
    cid = _chain_id()
    return f"QIE {'Mainnet' if cid == 1990 else 'Testnet'} ({cid})"

def _explorer_base() -> str:
    # explorer origin for the current chain — mainnet vs testnet are different hosts

    return "https://mainnet.qie.digital" if _chain_id() == 1990 else "https://testnet.qie.digital"

# rate limiting (in-memory, per wallet)
def _check_rate_limit(wallet: str) -> Optional[str]:
    # enforce per-wallet submission limits and detect boundary probing

    key = wallet.lower()
    now = time.time()

    # Check active cooldown first
    expiry = _rl_cooldown.get(key, 0)
    if now < expiry:
        remaining = int(expiry - now)
        return f"Rate limited — {remaining}s cooldown. Repeated failures detected."

    # Prune stale entries
    _rl_attempts[key] = [t for t in _rl_attempts[key] if now - t < 3600]
    _rl_failures[key] = [t for t in _rl_failures[key] if now - t < FAIL_WINDOW]

    # Hourly cap
    if len(_rl_attempts[key]) >= MAX_HOURLY:
        _rl_cooldown[key] = now + 3600
        return f"Rate limited — max {MAX_HOURLY} submissions/hour. Try again later."

    # Rapid-failure cooldown
    if len(_rl_failures[key]) >= 2:
        _rl_cooldown[key] = now + FAIL_COOLDOWN
        return "Rate limited — repeated failures in short window. 30-min cooldown applied."

    _rl_attempts[key].append(now)
    return None

def _record_outcome(wallet: str, passed: bool, anomaly_score: float) -> None:
    # update rate-limit state after a verification completes

    key = wallet.lower()
    if not passed:
        _rl_failures[key].append(time.time())
        _fraud_counts[key] += 1

    # Boundary-probe detection — track anomaly scores
    q = _rl_anomaly_q[key]
    q.append(anomaly_score)
    if len(q) > PROBE_WINDOW:
        q.pop(0)
    if len(q) >= 4 and sum(1 for s in q if 0.38 <= s <= 0.68) >= 4:
        log.warning(
            "BOUNDARY_PROBING suspected from %s — %d consecutive borderline anomaly scores",
            wallet[:12], len(q),
        )
        _fraud_counts[key] = max(_fraud_counts[key], 1)  # mark as suspicious

    _save_fraud_counts()   # persist strikes (survives restart)

def _submitter_reputation(wallet: str) -> Dict[str, Any]:
    # return this wallet's fraud history and reputation tier

    key        = wallet.lower()
    count      = _fraud_counts[key]
    reputation = "clean" if count == 0 else ("flagged" if count < 3 else "banned")
    remaining  = max(0, MAX_HOURLY - len([t for t in _rl_attempts[key] if time.time() - t < 3600]))

    return {
        "reputation":    reputation,
        "fraud_count":   count,
        "score_penalty": min(count * 3, 12),   # -3 pts per past fraud, max -12
        "remaining":     remaining,
    }

# retirement certificate generator
def _build_retirement_pdf(
    token_id:     int,
    project_id:   str,
    project_name: str,
    tonnes:       int,
    vintage:      int,
    retired_by:   str,
    retired_at:   str,
    tx_hash:      str,
    ipfs_cid:     Optional[str],
    beneficiary:  str = "",
    organization: str = "",
) -> Path:
    """Generate a verifiable PDF retirement certificate using ReportLab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle, Image

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    out = PDF_DIR / f"certificate_{token_id}.pdf"

    doc    = SimpleDocTemplate(str(out), pagesize=A4,
                               leftMargin=2.5*cm, rightMargin=2.5*cm,
                               topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    # leading set explicitly — the brand title and tagline were colliding without it.
    title  = ParagraphStyle("title",  fontSize=22, leading=26, fontName="Helvetica-Bold",
                             spaceAfter=2, textColor=colors.HexColor("#0d1b14"))
    sub    = ParagraphStyle("sub",    fontSize=10.5, leading=14, fontName="Helvetica",
                             textColor=colors.HexColor("#5a6b62"))
    green  = colors.HexColor("#00c853")

    # Header: logo on the left, brand + tagline stacked on the right.
    brand = [Paragraph("TerraLedger", title),
             Paragraph("Carbon Credit Retirement Certificate", sub)]
    logo_path = Path("assets/logo-icon.png")
    if logo_path.exists():
        logo = Image(str(logo_path), width=1.4*cm, height=1.4*cm)
        header = Table([[logo, brand]], colWidths=[1.7*cm, 14.8*cm])
        header.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",  (0, 0), (0, 0), 0),
            ("LEFTPADDING",  (1, 0), (1, 0), 6),
            ("TOPPADDING",   (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
        ]))
    else:
        header = Table([[brand]], colWidths=[16.5*cm])
        header.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    # "Retired on behalf of <name>, <org>" — only include the parts we actually have.
    claimant = ", ".join(p for p in (beneficiary, organization) if p)

    story = [
        header,
        Spacer(1, 0.45*cm),
        HRFlowable(width="100%", thickness=2, color=green),
        Spacer(1, 0.6*cm),

        Paragraph("CERTIFICATE OF RETIREMENT", ParagraphStyle(
            "cert", fontSize=14, fontName="Helvetica-Bold",
            textColor=green, spaceAfter=8)),
        Paragraph(
            f"This certifies that <b>{tonnes:,} tonne(s)</b> of verified CO<sub>2</sub> offsets "
            f"from project <b>{project_name}</b> have been permanently retired"
            + (f" on behalf of <b>{claimant}</b>" if claimant else "")
            + ". These credits cannot be resold, reused, or double-counted.",
            styles["Normal"]),
        Spacer(1, 0.6*cm),
    ]

    # Wrapping style for the value column — hashes and URLs have no spaces to break
    # on, so CJK word-wrap (break anywhere) is what keeps them inside the cell.
    val_st  = ParagraphStyle("val",  fontSize=9,   leading=12, fontName="Helvetica",
                             textColor=colors.HexColor("#111111"), wordWrap="CJK")
    mono_st = ParagraphStyle("mono", fontSize=8.5, leading=11, fontName="Courier",
                             textColor=colors.HexColor("#222222"), wordWrap="CJK")

    # bold label cells — needed so the CO2 subscript renders instead of a tofu box.
    lab_st  = ParagraphStyle("lab",  fontSize=9,   leading=12, fontName="Helvetica-Bold",
                             textColor=colors.HexColor("#666666"))

    def v(text):  return Paragraph(str(text), val_st)
    def m(text):  return Paragraph(str(text), mono_st)   # for hashes / long URLs

    audit_url = f"https://gateway.pinata.cloud/ipfs/{ipfs_cid}" if ipfs_cid else "—"
    data = [
        ["NFT Token ID",    v(token_id)],
        ["Project ID",      m(project_id)],
        ["Project Name",    v(project_name)],
        ["Vintage Year",    v(vintage)],
        [Paragraph("CO<sub>2</sub> Retired", lab_st), v(f"{tonnes:,} tonne(s)")],
        ["Beneficiary",     v(beneficiary or "—")],
        ["Organization",    v(organization or "—")],
        ["Retired By",      m(retired_by)],
        ["Retired At",      v(retired_at)],
        ["Retirement TX",   m(tx_hash)],
        ["AI Audit Report", m(audit_url)],
        ["Network",         v(_network_label())],
        ["Explorer",        m(f"{_explorer_base()}/tx/{tx_hash}")],
    ]
    table = Table(data, colWidths=[3.6*cm, 12.9*cm])
    table.setStyle(TableStyle([
        ("FONTNAME",    (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        ("TEXTCOLOR",   (0, 0), (0, -1), colors.HexColor("#666666")),
        ("BACKGROUND",  (0, 0), (-1, 0), colors.HexColor("#f8f8f8")),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
        ("GRID",        (0, 0), (-1, -1), 0.5, colors.HexColor("#e0e0e0")),
        ("VALIGN",      (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",  (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("WORDWRAP",    (1, 0), (1, -1), True),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.8*cm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#e0e0e0")))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(
        f"Generated by TerraLedger AI Verification System · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        ParagraphStyle("footer", fontSize=8, textColor=colors.HexColor("#aaaaaa")),
    ))

    doc.build(story)
    return out

# fastapi application
app = FastAPI(
    title       = "TerraLedger AI Verification API",
    version     = "1.0.0",
    description = (
        "AI-gated carbon credit verification for QIE Blockchain. "
        "Every submission passes 5 AI checks before an NFT can be minted."
    ),
)

# CORS — locked down for production. Set ALLOWED_ORIGINS (comma-separated) to your
# frontend domain(s) on mainnet; defaults to localhost dev origins only (no wildcard).
_origins_env = os.getenv("ALLOWED_ORIGINS", "").strip()
if _origins_env:
    _allow_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]
else:
    _allow_origins = [
        "http://localhost:8080", "http://127.0.0.1:8080",
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:5173", "http://127.0.0.1:5173",
        "http://localhost:5500", "http://127.0.0.1:5500",  # python -m http.server (README default)
    ]
    log.warning("ALLOWED_ORIGINS not set — CORS limited to localhost dev origins. "
                "Set ALLOWED_ORIGINS=<your-frontend-domain> for production/mainnet.")

app.add_middleware(
    CORSMiddleware,
    allow_origins  = _allow_origins,
    allow_methods  = ["GET", "POST", "OPTIONS"],
    allow_headers  = ["*"],
)

# get /health
@app.get("/health")
async def health() -> Dict[str, Any]:
    # live readiness probe. Reports each dependency's real status (not just whether

    # Ollama (audit LLM) — quick reachability ping
    ollama_up = False
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        ollama_up = r.status_code == 200
    except Exception:
        ollama_up = False

    # Chain connectivity
    chain_up = False
    try:
        chain_up = bool(_oracle._w3 and _oracle._w3.is_connected())
    except Exception:
        chain_up = False

    deps = {
        "spacy":            SPACY_AVAILABLE,
        "web3":             WEB3_AVAILABLE,
        "oracle_ready":     _oracle._ready,
        "chain_connected":  chain_up,
        "ollama_up":        ollama_up,            # live ping (audit falls back to template if down)
        "ollama_model":     OLLAMA_MODEL,
        "nasa_firms_key":   bool(os.getenv("NASA_FIRMS_API_KEY")),   # falls back to neutral score if missing
        "ipfs_pinata_key":  bool(os.getenv("PINATA_JWT")),          # audit still returned, just not pinned
        "qie_pass_onchain": bool(os.getenv("NEXT_PUBLIC_QIEPASS_ADDRESS")),  # testnet MockQIEPass demo
        "qie_pass_api":     _qiepass.ready,                         # real pass-api.qie.digital partner client
    }
    # Core = what must work to mint on-chain. Optional deps degrade gracefully.
    core_ok = deps["oracle_ready"] and deps["chain_connected"]
    status  = "ok" if core_ok else "degraded"

    return {
        "status":            status,
        "core_ready":        core_ok,
        "dependencies":      deps,
        "min_score":         MIN_SCORE,
        "hard_fail_overlap": HARD_FAIL_OVERLAP,
    }

# post /verify
@app.post("/verify", response_model=VerificationResult)
async def verify(
    project_id:          str            = Form(...),
    project_name:        str            = Form(...),
    gps_polygon:         str            = Form(...),   # JSON array of [lat, lon] pairs
    hectares:            float          = Form(...),
    tonnes_co2:          float          = Form(...),
    vintage_year:        int            = Form(...),
    project_type:        str            = Form(...),
    biome:               str            = Form(...),
    submitter_address:   str            = Form(...),
    registered_polygons: str            = Form(default="[]"),  # JSON from ProjectRegistry
    signature:           Optional[str]  = Form(default=None),  # wallet sig proving ownership
    signed_ts:           Optional[str]  = Form(default=None),  # ms timestamp inside the message
    document: Optional[UploadFile]      = File(default=None),
) -> VerificationResult:
    """
    Run all 5 AI verification modules and, on success, call the on-chain oracle.
    Also enforces per-wallet rate limits and checks submitter reputation.
    """
    t_start = time.time()
    flags:  List[str]     = []
    scores: Dict[str, int] = {}

    # ── Rate limiting ─────────────────────────────────────────────────────────
    limit_error = _check_rate_limit(submitter_address)
    if limit_error:
        raise HTTPException(status_code=429, detail=limit_error)

    # ── Submitter reputation ──────────────────────────────────────────────────
    rep = _submitter_reputation(submitter_address)
    if rep["reputation"] == "banned":
        raise HTTPException(
            status_code=403,
            detail=f"Wallet {submitter_address[:12]}… has {rep['fraud_count']} on-chain "
                   f"fraud strikes and is permanently banned from submitting."
        )
    if rep["fraud_count"] > 0:
        flags.append(f"REPEAT_OFFENDER: {rep['fraud_count']} prior fraud attempt(s) from this wallet")

    # ── Wallet-ownership proof ────────────────────────────────────────────────
    # The submitter signs "TerraLedger: verify project <id> as <wallet> @ <ts>".
    # We recover the signer and require it to match submitter_address (so a credit
    # can't be minted to / spam-submitted for a wallet you don't control).
    if signature and signed_ts:
        try:
            from eth_account.messages import encode_defunct
            msg = f"TerraLedger: verify project {project_id} as {submitter_address} @ {signed_ts}"
            recovered = EthAccount.recover_message(encode_defunct(text=msg), signature=signature)
            if recovered.lower() != submitter_address.lower():
                raise HTTPException(status_code=401, detail="Signature does not match the submitter wallet.")
            if abs(time.time() * 1000 - int(signed_ts)) > 600_000:   # 10-min freshness
                raise HTTPException(status_code=401, detail="Signature expired — please re-submit.")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid wallet signature.")
    else:
        flags.append("UNSIGNED_SUBMISSION")

    try:
        polygon  = json.loads(gps_polygon)
        existing = json.loads(registered_polygons)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON polygon: {exc}")

    # ── Module 1: GPS Overlap ─────────────────────────────────────────────────
    # Each module is wrapped fail-closed: a transient/internal error contributes
    # 0 points and a flag (never a false fraud accusation, never a 500) so the
    # endpoint always returns a structured verdict even if a dependency hiccups.
    try:
        gps_result = check_gps_overlap(polygon, existing)
    except Exception as exc:
        log.error("GPS module error (fail-closed): %s", exc)
        gps_result = {"hard_fail": False, "score": 0, "overlap_pct": 0.0,
                      "conflicting_ids": [], "reason": "GPS module unavailable — scored 0 (fail-closed)."}
        flags.append("GPS_MODULE_UNAVAILABLE")
    if gps_result["hard_fail"]:
        return _fail_response(
            project_id       = project_id,
            submitter        = submitter_address,
            reason           = f"GPS overlap {gps_result['overlap_pct']:.1f}%",
            scores           = {"gps": 0, "ownership": 0, "anomaly": 0, "satellite": 0},
            flags            = ["DUPLICATE_GPS_DETECTED"],
            details          = gps_result,
            t_start          = t_start,
            polygon          = polygon,
            hectares         = int(hectares),
            tonnes           = int(tonnes_co2),
        )
    scores["gps"] = gps_result["score"]
    if gps_result["overlap_pct"] > WARN_OVERLAP:
        flags.append("PARTIAL_GPS_OVERLAP")

    # ── Module 2: Ownership Document ─────────────────────────────────────────
    document_sha256 = ""   # full hash for the on-chain integrity proof
    if document:
        file_bytes = await document.read()
        document_sha256 = hashlib.sha256(file_bytes).hexdigest()
        try:
            doc_result = parse_ownership_document(file_bytes, document.filename or "", polygon, project_id)
        except Exception as exc:
            log.error("Ownership module error (fail-closed): %s", exc)
            doc_result = {"hard_fail": False, "score": 0, "owner_name": "Unknown",
                          "doc_gps": None, "gps_match": False, "confidence": 0.0,
                          "auth_flags": [], "reason": "Ownership module unavailable — scored 0 (fail-closed)."}
            flags.append("OWNERSHIP_MODULE_UNAVAILABLE")
        # Surface authenticity flags into the main flags list
        for af in doc_result.get("auth_flags", []):
            flags.append(af.split(":")[0])   # short flag code for the UI
    else:
        doc_result = {
            "hard_fail":  False,
            "score":      0,
            "owner_name": "Unknown",
            "doc_gps":    None,
            "gps_match":  False,
            "confidence": 0.0,
            "reason":     "No ownership document provided.",
        }
        flags.append("NO_OWNERSHIP_DOCUMENT")

    if doc_result["hard_fail"]:
        reason_low = doc_result.get("reason", "").lower()
        if "reuse" in reason_low:
            fail_reason, fail_flag = "Document reuse — deed already used for another project", "DOCUMENT_REUSE"
        elif "forg" in reason_low:
            fail_reason, fail_flag = "Forged document — not a genuine land deed", "FORGED_DOCUMENT"
        else:
            fail_reason, fail_flag = "Ownership document GPS mismatch", "OWNERSHIP_GPS_MISMATCH"
        return _fail_response(
            project_id = project_id,
            submitter  = submitter_address,
            reason     = fail_reason,
            scores     = {"gps": scores["gps"], "ownership": 0, "anomaly": 0, "satellite": 0},
            flags      = [fail_flag],
            details    = doc_result,
            t_start    = t_start,
            polygon    = polygon,
            hectares   = int(hectares),
            tonnes     = int(tonnes_co2),
        )
    scores["ownership"] = doc_result["score"]

    # ── Module 3: Anomaly Detection ───────────────────────────────────────────
    try:
        anomaly_result = check_anomaly(
            tonnes_co2   = tonnes_co2,
            hectares     = hectares,
            project_type = project_type,
            vintage_year = vintage_year,
            biome        = biome,
        )
    except Exception as exc:
        log.error("Anomaly module error (fail-closed): %s", exc)
        anomaly_result = {"score": 0, "is_outlier": False, "anomaly_score": 0.0,
                          "reason": "Anomaly module unavailable — scored 0 (fail-closed)."}
        flags.append("ANOMALY_MODULE_UNAVAILABLE")
    # Reputation penalty: flagged wallets get anomaly score capped at 15/25
    raw_anomaly_score = anomaly_result["score"]
    if rep["fraud_count"] > 0:
        anomaly_result["score"] = min(raw_anomaly_score, 25 - rep["score_penalty"])
    scores["anomaly"] = anomaly_result["score"]
    if anomaly_result["is_outlier"]:
        flags.append("ANOMALOUS_CARBON_CLAIM")

    # ── Module 4: NASA Satellite ──────────────────────────────────────────────
    try:
        satellite_result = check_nasa_firms(polygon)
    except Exception as exc:
        log.error("Satellite module error (fail-closed): %s", exc)
        satellite_result = {"score": 0, "fire_count": 0, "alert_density": 0.0,
                            "permanence_risk": "unknown",
                            "reason": "Satellite module unavailable — scored 0 (fail-closed)."}
        flags.append("SATELLITE_MODULE_UNAVAILABLE")
    scores["satellite"] = satellite_result["score"]
    risk = satellite_result["permanence_risk"]
    if risk == "high":
        flags.append("HIGH_PERMANENCE_RISK")
    elif risk == "medium":
        flags.append("MEDIUM_PERMANENCE_RISK")

    # ── Scoring ───────────────────────────────────────────────────────────────
    total   = sum(scores.values())
    verdict = "PASS" if total >= MIN_SCORE else "FAIL"

    # ── Module 5: Audit Report ────────────────────────────────────────────────
    report_data = {
        "project_id":       project_id,
        "project_name":     project_name,
        "submitter":        submitter_address,
        "scores":           scores,
        "total_score":      total,
        "verdict":          verdict,
        "flags":            flags,
        "gps_result":       gps_result,
        "doc_result":       doc_result,
        "anomaly_result":   anomaly_result,
        "satellite_result": satellite_result,
        "tonnes_co2":       tonnes_co2,
        "hectares":         hectares,
        "vintage_year":     vintage_year,
        "project_type":     project_type,
        "biome":            biome,
    }
    report_text = generate_audit_report(report_data)
    pdf_path    = generate_audit_pdf(report_text, project_id)
    ipfs_cid    = upload_to_ipfs(report_text, project_id, scores, verdict, total)

    # ── On-chain oracle call (registers + verifies + mints) ───────────────────
    tx_hash = _oracle.submit(
        project_id          = project_id,
        gps_score           = scores["gps"],
        ownership_score     = scores["ownership"],
        anomaly_score       = scores["anomaly"],
        satellite_score     = scores["satellite"],
        gps_hard_fail       = False,
        ownership_hard_fail = False,
        report_cid          = ipfs_cid or "",
        flags               = flags,
        vintage             = vintage_year,
        tonnes              = int(tonnes_co2),
        polygon             = polygon,
        hectares            = int(hectares),
        recipient           = submitter_address,
        is_pass             = verdict == "PASS",
        doc_hash            = document_sha256,
    )

    # Surface a silent mint failure: AI approved but the on-chain mint didn't
    # confirm (gas / RPC / nonce). The user must know no NFT exists yet.
    if verdict == "PASS" and not tx_hash:
        flags.append("MINT_NOT_CONFIRMED")

    # ── Persist redacted extended docs (QIE Pass-gated retrieval later) ────────
    if verdict == "PASS" and document:
        _save_extended_doc(project_id, project_name, doc_result)

    # ── Record the deed fingerprint ONLY when a credit actually minted ─────────
    # (so a failed mint / transient error never consumes the deed → safe retries)
    if verdict == "PASS" and tx_hash and document:
        _record_doc_fingerprint(document_sha256, doc_result.get("owner_name", "Unknown"),
                                doc_result.get("doc_gps"), project_id)

    # ── Record outcome for rate-limit + probe tracking ────────────────────────
    _record_outcome(submitter_address, verdict == "PASS", anomaly_result["anomaly_score"])

    elapsed_ms = int((time.time() - t_start) * 1000)
    log.info(
        "[%s] %s score=%d/%d rep=%s flags=%s time=%dms tx=%s",
        project_id, verdict, total, 100, rep["reputation"], flags, elapsed_ms,
        tx_hash[:10] if tx_hash else "none",
    )

    return VerificationResult(
        project_id              = project_id,
        submitter               = submitter_address,
        verdict                 = verdict,
        total_score             = total,
        scores                  = scores,
        flags                   = flags,
        owner_name              = doc_result.get("owner_name", "Unknown"),
        gps_overlap_pct         = gps_result["overlap_pct"],
        conflicting_project_ids = gps_result.get("conflicting_ids", []),
        anomaly_score           = anomaly_result["anomaly_score"],
        fire_count              = satellite_result["fire_count"],
        permanence_risk         = satellite_result["permanence_risk"],
        report_text             = report_text,
        report_ipfs_cid         = ipfs_cid,
        report_pdf_path         = str(pdf_path) if pdf_path else None,
        on_chain_tx             = tx_hash,
        processing_time_ms      = elapsed_ms,
        should_mint             = verdict == "PASS",
        reject_reason           = None,
        submitter_reputation    = rep["reputation"],
        submitter_fraud_history = rep["fraud_count"],
        rate_limit_remaining    = rep["remaining"] - 1,
    )

def _fail_response(
    project_id: str,
    submitter:  str,
    reason:     str,
    scores:     Dict[str, int],
    flags:      List[str],
    details:    Dict[str, Any],
    t_start:    float,
    polygon:    Optional[List[List[float]]] = None,
    hectares:   int = 0,
    tonnes:     int = 0,
) -> VerificationResult:
    """Construct a hard-fail response and log the fraud attempt on-chain."""
    log.warning("[%s] HARD FAIL — %s", project_id, reason)

    # Register + flag as fraud on-chain (oracle calls flagFraud via submitVerification)
    tx_hash = _oracle.submit(
        project_id          = project_id,
        gps_score           = scores.get("gps", 0),
        ownership_score     = scores.get("ownership", 0),
        anomaly_score       = scores.get("anomaly", 0),
        satellite_score     = scores.get("satellite", 0),
        gps_hard_fail       = "GPS" in reason.upper() or "DUPLICATE" in reason.upper(),
        ownership_hard_fail = any(k in reason.upper() for k in
                                  ("OWNERSHIP", "MISMATCH", "FORG", "REUSE", "DOCUMENT")),
        report_cid          = "",
        flags               = flags,
        vintage             = 0,
        tonnes              = max(int(tonnes), 1),
        polygon             = polygon,
        hectares            = int(hectares),
    )

    # Mark fraud in reputation tracking
    _record_outcome(submitter, False, 1.0)

    return VerificationResult(
        project_id              = project_id,
        submitter               = submitter,
        verdict                 = "FAIL",
        total_score             = 0,
        scores                  = scores,
        flags                   = flags,
        owner_name              = "Unknown",
        gps_overlap_pct         = details.get("overlap_pct", 0.0),
        conflicting_project_ids = details.get("conflicting_ids", []),
        anomaly_score           = 0.0,
        fire_count              = 0,
        permanence_risk         = "unknown",
        report_text             = f"REJECTED — {reason}. Fraud attempt logged on QIE blockchain.",
        report_ipfs_cid         = None,
        report_pdf_path         = None,
        on_chain_tx             = tx_hash,
        processing_time_ms      = int((time.time() - t_start) * 1000),
        should_mint             = False,
        reject_reason           = reason,
        submitter_reputation    = "flagged",
        submitter_fraud_history = _fraud_counts.get(submitter.lower(), 0),
        rate_limit_remaining    = max(0, MAX_HOURLY - len(_rl_attempts.get(submitter.lower(), []))),
    )

# post /retirement-certificate
@app.post("/retirement-certificate")
async def retirement_certificate(
    token_id:     int            = Form(...),
    project_id:   str            = Form(...),
    project_name: str            = Form(...),
    tonnes:       int            = Form(...),
    vintage:      int            = Form(...),
    retired_by:   str            = Form(...),
    retired_at:   str            = Form(...),
    tx_hash:      str            = Form(...),
    ipfs_cid:     Optional[str]  = Form(default=None),
    beneficiary:  Optional[str]  = Form(default=""),
    organization: Optional[str]  = Form(default=""),
) -> FileResponse:
    """
    Generate the PDF retirement certificate after a credit is burned on-chain.
    Frontend calls this once CarbonCredit.retire() confirms. Beneficiary +
    organization (who's claiming the offset) come from the retire form.
    """
    try:
        pdf_path = _build_retirement_pdf(
            token_id, project_id, project_name,
            tonnes, vintage, retired_by, retired_at, tx_hash, ipfs_cid,
            beneficiary or "", organization or "",
        )
        return FileResponse(
            path         = str(pdf_path),
            filename     = f"terraledger_certificate_{token_id}.pdf",
            media_type   = "application/pdf",
        )
    except Exception as exc:
        log.error("Certificate generation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Certificate error: {exc}")

# get /marketplace
@app.get("/marketplace")
async def marketplace() -> Dict[str, Any]:
    # return on-chain approved project data for the frontend marketplace

    if not _oracle._ready or not _oracle._w3:
        return {"credits": [], "source": "oracle_not_configured"}

    REGISTRY_ABI = [
        {"inputs": [], "name": "getApprovedProjectIds",
         "outputs": [{"type": "bytes32[]"}], "stateMutability": "view", "type": "function"},
        {"inputs": [{"name": "id", "type": "bytes32"}], "name": "getProject",
         "outputs": [{"components": [
             {"name": "id", "type": "bytes32"}, {"name": "owner", "type": "address"},
             {"name": "polygonGeoJSON", "type": "string"}, {"name": "areaHectares", "type": "uint256"},
             {"name": "claimedTonnes", "type": "uint256"}, {"name": "submittedAt", "type": "uint256"},
             {"name": "status", "type": "uint8"}, {"name": "statusReason", "type": "string"},
         ], "type": "tuple"}], "stateMutability": "view", "type": "function"},
    ]

    try:
        from web3 import Web3
        registry_addr = os.getenv("NEXT_PUBLIC_PROJECT_REGISTRY_ADDRESS")
        if not registry_addr:
            return {"credits": [], "source": "no_registry_address"}

        contract  = _oracle._w3.eth.contract(
            address=Web3.to_checksum_address(registry_addr), abi=REGISTRY_ABI
        )
        ids       = contract.functions.getApprovedProjectIds().call()
        credits   = []
        for raw_id in ids[:20]:  # cap at 20 for latency
            proj = contract.functions.getProject(raw_id).call()
            credits.append({
                "id":           "0x" + raw_id.hex(),
                "owner":        proj[1],
                "area_hectares": proj[3],
                "claimed_tonnes": proj[4],
                "submitted_at": proj[5],
                "status":       ["Pending","Approved","Rejected","FraudFlagged"][proj[6]],
            })
        return {"credits": credits, "source": "on_chain", "count": len(credits)}
    except Exception as exc:
        log.error("Marketplace query failed: %s", exc)
        return {"credits": [], "source": "error", "error": str(exc)}

# developer read api (for third-party integrations)
# Minimal ABIs reused by the public read endpoints
_CREDIT_READ_ABI = [
    {"inputs": [], "name": "totalMinted", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "credits",
     "outputs": [
         {"name": "projectId", "type": "bytes32"}, {"name": "vintage", "type": "uint256"},
         {"name": "tonnes", "type": "uint256"}, {"name": "ipfsCid", "type": "string"},
         {"name": "retired", "type": "bool"}, {"name": "retiredAt", "type": "uint256"},
         {"name": "retiredBy", "type": "address"},
         {"name": "score", "type": "uint16"}, {"name": "gpsScore", "type": "uint8"},
         {"name": "ownershipScore", "type": "uint8"}, {"name": "anomalyScore", "type": "uint8"},
         {"name": "satelliteScore", "type": "uint8"},
     ], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenId", "type": "uint256"}], "name": "ownerOf",
     "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]
_ORACLE_READ_ABI = [
    {"inputs": [{"name": "projectId", "type": "bytes32"}], "name": "getDocumentHash",
     "outputs": [{"type": "bytes32"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getFraudAttempts", "outputs": [{"type": "bytes32[]"}],
     "stateMutability": "view", "type": "function"},
]

@app.get("/api/stats")
async def api_stats() -> Dict[str, Any]:
    # public live protocol stats, read straight from the QIE testnet contracts

    if not _oracle._ready or not _oracle._w3:
        return {"error": "oracle not configured"}
    from web3 import Web3
    try:
        credit = _oracle._w3.eth.contract(
            address=Web3.to_checksum_address(os.getenv("NEXT_PUBLIC_CARBON_CREDIT_ADDRESS")),
            abi=_CREDIT_READ_ABI)
        oracle = _oracle._w3.eth.contract(
            address=Web3.to_checksum_address(os.getenv("ORACLE_CONTRACT_ADDRESS")),
            abi=_ORACLE_READ_ABI)
        minted = credit.functions.totalMinted().call()
        tonnes = 0
        for i in range(minted):
            try: tonnes += credit.functions.credits(i).call()[2]
            except Exception: pass
        try: frauds = len(oracle.functions.getFraudAttempts().call())
        except Exception: frauds = 0
        return {
            "credits_verified": minted,
            "frauds_blocked":   frauds,
            "tonnes_co2":       tonnes,
            "double_counted":   0,
            "network":          _network_label(),
        }
    except Exception as exc:
        return {"error": str(exc)}

@app.get("/api/credit/{token_id}")
async def api_credit(token_id: int) -> Dict[str, Any]:
    # public credential for one credit — lets any external app verify a TerraLedger

    if not _oracle._ready or not _oracle._w3:
        return {"error": "oracle not configured"}
    from web3 import Web3
    try:
        credit = _oracle._w3.eth.contract(
            address=Web3.to_checksum_address(os.getenv("NEXT_PUBLIC_CARBON_CREDIT_ADDRESS")),
            abi=_CREDIT_READ_ABI)
        oracle = _oracle._w3.eth.contract(
            address=Web3.to_checksum_address(os.getenv("ORACLE_CONTRACT_ADDRESS")),
            abi=_ORACLE_READ_ABI)

        m = credit.functions.credits(token_id).call()
        (project_id, vintage, tonnes, ipfs_cid, retired, retired_at, retired_by,
         score, gps_s, own_s, anom_s, sat_s) = m
        pid_hex = "0x" + project_id.hex()

        holder = None
        if not retired:
            try: holder = credit.functions.ownerOf(token_id).call()
            except Exception: pass

        doc_hash = "0x" + oracle.functions.getDocumentHash(project_id).call().hex()

        # AI audit from IPFS (scores, verdict)
        audit = None
        if ipfs_cid:
            try:
                r = requests.get(f"https://gateway.pinata.cloud/ipfs/{ipfs_cid}", timeout=10)
                audit = r.json()
            except Exception:
                audit = None

        return {
            "token_id":     token_id,
            "project_id":   pid_hex,
            "vintage":      vintage,
            "tonnes_co2":   tonnes,
            "retired":      retired,
            "retired_by":   retired_by if retired else None,
            "holder":       holder,
            "ipfs_cid":     ipfs_cid,
            "ipfs_url":     f"https://gateway.pinata.cloud/ipfs/{ipfs_cid}" if ipfs_cid else None,
            "document_hash": doc_hash,
            "on_chain_score": {
                "total":     score,
                "gps":       gps_s,
                "ownership": own_s,
                "anomaly":   anom_s,
                "satellite": sat_s,
            },
            "ai_audit": None if not audit else {
                "verdict":     audit.get("verdict"),
                "total_score": audit.get("total_score"),
                "scores":      audit.get("scores"),
            },
            "network": _network_label(),
        }
    except Exception as exc:
        return {"error": str(exc), "token_id": token_id}

@app.get("/document-access")
async def document_access(project_id: str, wallet: str) -> Dict[str, Any]:
    # qIE Pass-gated deep document access (Phase 2)

    granted = _oracle.has_document_access(project_id, wallet)
    if granted is None:
        raise HTTPException(
            status_code=503,
            detail="On-chain oracle unreachable — cannot verify QIE Pass document grant.",
        )
    if not granted:
        raise HTTPException(
            status_code=403,
            detail=(
                "No document-access grant for this wallet. A QIE Pass-verified buyer "
                "must call CarbonOracle.requestDocumentAccess(projectId) first."
            ),
        )

    try:
        store = json.loads(DOC_ACCESS_PATH.read_text()) if DOC_ACCESS_PATH.exists() else {}
    except Exception:
        store = {}

    record = store.get(project_id)
    if not record:
        return {
            "project_id": project_id,
            "access":     "granted",
            "extended_documents": None,
            "note":       "Access granted, but no extended document record is stored for this project.",
        }

    return {
        "project_id":          project_id,
        "access":              "granted",
        "qie_pass_verified":   True,
        "extended_documents":  record,
        "privacy_note":        "Owner name is redacted (PII). Raw deed never leaves secure storage; "
                               "the on-chain doc hash proves integrity without exposing the document.",
        "network":             _network_label(),
    }

# qie pass — real identity verification (rest api partner flow)
# These drive the production QIE Pass flow (pass-api.qie.digital). The frontend
# calls them to verify a corporate buyer's real identity (with consent + ZK/
# selective disclosure) instead of the testnet on-chain MockQIEPass self-attest.

class QIEPassVerifyBody(BaseModel):
    identifier: str                         # did:qie:... OR a wallet address
    # Verified live against pass-api.qie.digital — valid claims include firstName,
    # lastName, age_over_21 (country/email/fullName are NOT valid claim names).
    claims:     List[str] = ["firstName", "lastName", "age_over_21"]

class QIEPassClaimBody(BaseModel):
    request_id: str
    wallet:     Optional[str] = None   # connected wallet to mark verified on-chain

@app.post("/qiepass/verify-request")
async def qiepass_verify_request(body: QIEPassVerifyBody) -> Dict[str, Any]:
    # step 1 — create a QIE Pass verification request for a buyer. If they are

    return _qiepass.create_verification_request(body.identifier, body.claims)

@app.get("/qiepass/status/{request_id}")
async def qiepass_status(request_id: str) -> Dict[str, Any]:
    # step 2 — poll until status is 'consent_given' and vcMetadata.ready is true

    return _qiepass.get_request_status(request_id)

@app.post("/qiepass/claim")
async def qiepass_claim(body: QIEPassClaimBody) -> Dict[str, Any]:
    # step 3 — claim the verified credential (selective-disclosure claims + ECDSA

    data = _qiepass.claim_and_verify(body.request_id)
    if data.get("fully_valid") and body.wallet:
        claims = data.get("requestedClaims") or {}
        name = (str(claims.get("firstName", "")) + " " + str(claims.get("lastName", ""))).strip() or "QIE Pass holder"
        org  = claims.get("organization") or "QIE Pass verified"
        data["onchain_attestation_tx"] = _oracle.attest_identity(body.wallet, name, org)
    return data

# entry point
if __name__ == "__main__":
    print()
    print("  TerraLedger AI Backend")
    print("  ─────────────────────────────────────────")
    print("  Swagger UI  : http://localhost:8000/docs")
    print("  Health      : http://localhost:8000/health")
    print("  Oracle ready:", _oracle._ready)
    print()
    uvicorn.run(
        "terraledger:app",
        host    = "0.0.0.0",
        port    = 8000,
        reload  = True,
    )
