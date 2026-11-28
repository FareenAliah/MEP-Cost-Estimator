from datetime import datetime
import os
from pathlib import Path
import re
import sys

import pandas as pd

# =====================================================================
# SETTINGS (change here, not inside the code below)
# =====================================================================
SERVICES = ("acmv", "fire", "plumbing", "lift")
DEFAULT_INFLATION_RATE = 0.03  # 3% compounding annual inflation rate
SAFETY_FACTOR = 0.00         # Set e.g. 0.10 for 10% safety factor, or 0.00 to disable
NEAREST_K = 2                # Number of nearest past projects averaged
CURRENCY = "MYR"

# =====================================================================
# MODULE 1: PDF AREA & HEIGHT EXTRACTION
# =====================================================================
# Matches e.g. "GFA: 12,500.50 m2", "Gross Floor Area = 4500 sqm", "Area (m²): 1200"
AREA_PATTERN = re.compile(
    r"(?:GFA|GROSS\s+FLOOR\s+AREA|TOTAL\s+AREA|FLOOR\s+AREA|AREA)"
    r"\s*(?:\([^)]*\))?"
    r"\s*[:=\-]?\s*"
    r"([\d,]+\.?\d*)"
    r"\s*(?:m2|m²|sqm|sq\.?\s?m)?",
    re.IGNORECASE,
)

# Matches e.g. "Building Height: 85.5 m", "Height = 42 metres", "Max Height: 30m"
HEIGHT_PATTERN = re.compile(
    r"(?:BUILDING\s+HEIGHT|BLDG\.?\s+HEIGHT|MAX(?:IMUM)?\s+HEIGHT|OVERALL\s+HEIGHT|HEIGHT)"
    r"\s*(?:\([^)]*\))?"
    r"\s*[:=\-]?\s*"
    r"([\d,]+\.?\d*)"
    r"\s*(?:m|metres?|meters?)\b",
    re.IGNORECASE,
)


def extract_measurements_from_pdf(pdf_path):
    """
    Return (areas_list, heights_list) where each item is (page_number, value).
    If reading fails or nothing is found, returns empty lists so caller can prompt manually.
    """
    if not os.path.exists(pdf_path):
        print(f"[Warning] PDF file not found: {pdf_path}. Falling back to manual input.")
        return [], []

    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("[Warning] PyMuPDF not installed (pip install pymupdf). Falling back to manual input.")
        return [], []

    areas_found = []
    heights_found = []

    doc = None
    try:
        doc = fitz.open(pdf_path)
        for page_num in range(len(doc)):
            text = doc[page_num].get_text("text") or ""

            for match in AREA_PATTERN.findall(text):
                try:
                    val = float(match.replace(",", "").rstrip("."))
                except ValueError:
                    continue
                if val > 0:
                    areas_found.append((page_num + 1, val))

            for match in HEIGHT_PATTERN.findall(text):
                try:
                    val = float(match.replace(",", "").rstrip("."))
                except ValueError:
                    continue
                if 0 < val < 2000:
                    heights_found.append((page_num + 1, val))
    except Exception as err:
        print(f"[Warning] Failed to read PDF '{pdf_path}': {err}. Falling back to manual input.")
        return [], []
    finally:
        if doc is not None:
            doc.close()

    if not areas_found:
        print("[Note] Could not detect area callout in PDF (scanned PDFs need OCR or manual entry).")
    if not heights_found:
        print("[Note] Could not detect height callout in PDF (scanned PDFs need OCR or manual entry).")

    return areas_found, heights_found


def select_or_manual_measurement(label, unit, candidates, manual_prompt):
    """
    Let the user pick a detected value from PDF candidates, or enter it manually
    if extraction failed, was empty, or the user declines the detected values.
    """
    if candidates:
        print(f"\nDetected {label} candidates in PDF:")
        for idx, (page, val) in enumerate(candidates, start=1):
            print(f"  {idx}. Page {page}: {val:,.2f} {unit}")
        while True:
            choice = input(
                f"Select {label} number (1-{len(candidates)}), or press Enter for manual input: "
            ).strip()
            if not choice:
                break
            if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                selected = candidates[int(choice) - 1][1]
                print(f"  Using PDF {label}: {selected:,.2f} {unit}")
                return selected
            print("  Invalid selection.")

    return ask_positive_float(manual_prompt)


# =====================================================================
# MODULE 2: BUILDING TYPES, INFLATION & LIFT SUPPLIER FALLBACK
# =====================================================================
KNOWN_BUILDING_TYPES = ("hotel", "apartment", "office", "commercial")


def normalize_building_type(raw_type):
    cleaned = raw_type.strip().lower()
    if cleaned in ("apartments", "apartment", "resi", "residential"):
        return "apartment"
    if cleaned in ("hotels", "hotel"):
        return "hotel"
    if cleaned in ("offices", "office"):
        return "office"
    if cleaned in ("commercial", "retail", "mall"):
        return "commercial"
    return cleaned


def apply_inflation(amount, base_year, target_year, rate=DEFAULT_INFLATION_RATE):
    years_diff = max(0, int(target_year) - int(base_year))
    return float(amount) * ((1.0 + float(rate)) ** years_diff)


def apply_safety_factor(amount, safety_factor=SAFETY_FACTOR):
    return float(amount) * (1.0 + max(0.0, float(safety_factor)))


def get_lift_cost_with_supplier_fallback(
    past_df,
    building_type,
    height_m,
    area_sqm,
    year_construction,
    units_or_rooms,
    est_lifts,
    inflation_rate=DEFAULT_INFLATION_RATE,
):
    if not est_lifts or est_lifts <= 0:
        return None, "not applicable"

    past_cost = estimate_service_cost(
        past_df=past_df,
        service="lift",
        building_type=building_type,
        height_m=height_m,
        area_sqm=area_sqm,
        year_construction=year_construction,
        units_or_rooms=units_or_rooms,
        est_lifts=est_lifts,
        inflation_rate=inflation_rate,
    )
    if past_cost is not None:
        return past_cost, "past projects (inflated)"

    print(f"\nNo past lift cost data found in file. Estimated {est_lifts} lift(s) required.")
    raw_quote = input(
        "Enter supplier installed cost per lift incl. installation (or 'not applicable'): "
    ).strip().lower()

    if raw_quote in ("not applicable", "na", "n/a", "none", ""):
        return None, "not applicable"

    supplier_rate = float(raw_quote.replace(",", ""))
    if supplier_rate <= 0:
        return None, "not applicable"

    is_current = input(
        "Is this supplier quote at current year price? (yes / no): "
    ).strip().lower()

    if is_current in ("yes", "y"):
        quote_year = datetime.now().year
    else:
        quote_year = ask_positive_int("Enter supplier quote year (e.g., 2025): ")

    total_quote = supplier_rate * est_lifts
    adjusted_cost = apply_inflation(total_quote, quote_year, year_construction, inflation_rate)
    return adjusted_cost, f"supplier quote ({quote_year} -> {year_construction})"


# =====================================================================
# MODULE 3: INPUT HELPERS
# =====================================================================
def ask_positive_float(prompt):
    while True:
        raw = input(prompt).strip().replace(",", "")
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        print("  Please enter a valid number greater than 0.")


def ask_positive_int(prompt):
    while True:
        raw = input(prompt).strip().replace(",", "")
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        print("  Please enter a valid whole number greater than 0.")


def ask_non_negative_int(prompt):
    while True:
        raw = input(prompt).strip().replace(",", "")
        try:
            val = int(raw)
            if val >= 0:
                return val
        except ValueError:
            pass
        print("  Please enter a valid whole number (0 or greater).")


def ask_building_type(prompt):
    while True:
        raw = input(prompt).strip()
        normalized = normalize_building_type(raw)
        if normalized in KNOWN_BUILDING_TYPES:
            return normalized
        print(f"  Please enter one of: {', '.join(KNOWN_BUILDING_TYPES)}")


def ask_service_applicable(service):
    reply = input(
        f"Is '{service.upper()}' applicable? (yes / not applicable): "
    ).strip().lower()
    return reply not in ("not applicable", "na", "n/a", "no", "none")


# =====================================================================
# MODULE 4: HISTORICAL DATA LOADER & COST ENGINE
# =====================================================================
def load_past_projects(file_path):
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path)
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        raise ValueError(f"Unsupported file format '{suffix}'. Use .csv, .xlsx, or .xls.")

    df.columns = [col.strip().lower() for col in df.columns]
    return df


def _compute_nearest_rows(df, building_type, height_m, area_sqm, units_or_rooms=None, k=NEAREST_K):
    matched = df[df["building_type"].astype(str).str.lower() == building_type].copy()
    if matched.empty:
        matched = df.copy()

    use_units = building_type in ("hotel", "apartment")
    target_units = 0.0 if (not use_units or units_or_rooms is None) else float(units_or_rooms)

    max_h = max(df["height_m"].fillna(0).max(), 1.0)
    max_a = max(df["area_sqm"].fillna(0).max(), 1.0)
    max_u = max(df["units_or_rooms"].fillna(0).max() if "units_or_rooms" in df.columns else 0, 1.0)

    h_diff = ((matched["height_m"].fillna(0) - height_m) / max_h) ** 2
    a_diff = ((matched["area_sqm"].fillna(0) - area_sqm) / max_a) ** 2

    if use_units and "units_or_rooms" in matched.columns:
        past_units = matched["units_or_rooms"].fillna(0)
        u_diff = ((past_units - target_units) / max_u) ** 2
        matched["distance"] = (h_diff + a_diff + u_diff) ** 0.5
    else:
        matched["distance"] = (h_diff + a_diff) ** 0.5

    return matched.nsmallest(k, "distance").copy()


def estimate_lift_count(past_df, building_type, height_m, area_sqm, units_or_rooms=None):
    if "service" not in past_df.columns or "lifts" not in past_df.columns:
        return 0

    lift_rows = past_df[
        (past_df["service"].astype(str).str.lower() == "lift") & (past_df["lifts"].notna())
    ].copy()
    if lift_rows.empty:
        return 0

    nearest = _compute_nearest_rows(
        lift_rows,
        building_type=building_type,
        height_m=height_m,
        area_sqm=area_sqm,
        units_or_rooms=units_or_rooms,
        k=NEAREST_K,
    )
    return round(float(nearest["lifts"].mean()))


def estimate_service_cost(
    past_df,
    service,
    building_type,
    height_m,
    area_sqm,
    year_construction,
    units_or_rooms=None,
    est_lifts=None,
    inflation_rate=DEFAULT_INFLATION_RATE,
):
    required = {"service", "awarded_cost", "year_awarded"}
    if not required.issubset(set(past_df.columns)):
        return None

    svc_df = past_df[
        (past_df["service"].astype(str).str.lower() == service)
        & (past_df["awarded_cost"].notna())
        & (past_df["awarded_cost"] > 0)
        & (past_df["year_awarded"].notna())
    ].copy()

    if svc_df.empty:
        return None

    nearest = _compute_nearest_rows(
        svc_df,
        building_type=building_type,
        height_m=height_m,
        area_sqm=area_sqm,
        units_or_rooms=units_or_rooms,
        k=NEAREST_K,
    )

    nearest["inflated_cost"] = nearest.apply(
        lambda r: apply_inflation(
            amount=float(r["awarded_cost"]),
            base_year=int(r["year_awarded"]),
            target_year=year_construction,
            rate=inflation_rate,
        ),
        axis=1,
    )

    if service == "lift":
        if "lifts" not in nearest.columns:
            return None
        valid_lifts = nearest[nearest["lifts"].fillna(0) > 0].copy()
        if valid_lifts.empty or not est_lifts:
            return None
        valid_lifts["cost_per_lift"] = valid_lifts["inflated_cost"] / valid_lifts["lifts"]
        avg_cost_per_lift = valid_lifts["cost_per_lift"].mean()
        return float(avg_cost_per_lift * est_lifts)

    nearest["area_ratio"] = nearest["area_sqm"].apply(
        lambda a: (area_sqm / float(a)) if pd.notna(a) and float(a) > 0 else 1.0
    )
    nearest["scaled_cost"] = nearest["inflated_cost"] * nearest["area_ratio"]
    return float(nearest["scaled_cost"].mean())


# =====================================================================
# MODULE 5: MAIN WORKFLOW & REPORT
# =====================================================================
def run_mep_estimator():
    print("=== MEP COST ESTIMATION SYSTEM ===\n")

    # 1. Load past awarded cost dataset
    file_path = (
        sys.argv[1]
        if len(sys.argv) >= 2
        else input("1. Path to past awarded cost file (.xlsx or .csv): ").strip().strip('"')
    )
    try:
        past_df = load_past_projects(file_path)
    except (FileNotFoundError, ValueError) as err:
        print(f"Error loading file: {err}")
        sys.exit(1)

    # 2. Optional PDF extraction for area & height (manual fallback if missing/failed)
    areas_found = []
    heights_found = []
    pdf_path = input(
        "2. Path to PDF drawing for area & height (or press Enter for manual input): "
    ).strip().strip('"')
    if pdf_path:
        areas_found, heights_found = extract_measurements_from_pdf(pdf_path)

    building_type = ask_building_type(
        "3. Building type (hotel / apartment / office / commercial): "
    )

    area_sqm = select_or_manual_measurement(
        label="area",
        unit="m2",
        candidates=areas_found,
        manual_prompt="4. Gross floor area (m2): ",
    )
    height_m = select_or_manual_measurement(
        label="height",
        unit="m",
        candidates=heights_found,
        manual_prompt="5. Building height (m): ",
    )
    year_construction = ask_positive_int("6. Target year of construction (e.g., 2027): ")

    units_or_rooms = None
    if building_type in ("hotel", "apartment"):
        units_or_rooms = ask_non_negative_int("7. Number of units or rooms: ")

    # 3. Check applicable services
    print("\n8. Select applicable MEP services:")
    applicable_services = {svc: ask_service_applicable(svc) for svc in SERVICES}

    est_lifts = None
    if applicable_services["lift"]:
        est_lifts = estimate_lift_count(
            past_df=past_df,
            building_type=building_type,
            height_m=height_m,
            area_sqm=area_sqm,
            units_or_rooms=units_or_rooms,
        )
        if est_lifts <= 0:
            est_lifts = ask_non_negative_int(
                "No past lift count data found. Enter estimated number of lifts (0 if none): "
            )

        if est_lifts <= 0:
            print("Lift count is 0 -> marking Lift as 'not applicable'.")
            applicable_services["lift"] = False
        else:
            print(f"Estimated number of lifts: {est_lifts}")

    # 4. Run cost estimation per service
    results = []
    grand_total = 0.0

    for service in SERVICES:
        if not applicable_services[service]:
            results.append(
                {
                    "service": service.upper(),
                    "status": "not applicable",
                    "source": "not applicable",
                    "estimated_cost": None,
                }
            )
            continue

        if service == "lift":
            raw_cost, source = get_lift_cost_with_supplier_fallback(
                past_df=past_df,
                building_type=building_type,
                height_m=height_m,
                area_sqm=area_sqm,
                year_construction=year_construction,
                units_or_rooms=units_or_rooms,
                est_lifts=est_lifts,
                inflation_rate=DEFAULT_INFLATION_RATE,
            )
            if raw_cost is None:
                results.append(
                    {
                        "service": service.upper(),
                        "status": "not applicable",
                        "source": "not applicable",
                        "estimated_cost": None,
                    }
                )
            else:
                final_cost = apply_safety_factor(raw_cost, SAFETY_FACTOR)
                grand_total += final_cost
                results.append(
                    {
                        "service": service.upper(),
                        "status": "ok",
                        "source": source,
                        "estimated_cost": final_cost,
                    }
                )
        else:
            raw_cost = estimate_service_cost(
                past_df=past_df,
                service=service,
                building_type=building_type,
                height_m=height_m,
                area_sqm=area_sqm,
                year_construction=year_construction,
                units_or_rooms=units_or_rooms,
                est_lifts=est_lifts,
                inflation_rate=DEFAULT_INFLATION_RATE,
            )
            if raw_cost is None:
                results.append(
                    {
                        "service": service.upper(),
                        "status": "error",
                        "source": "error, previous data not found",
                        "estimated_cost": None,
                    }
                )
            else:
                final_cost = apply_safety_factor(raw_cost, SAFETY_FACTOR)
                grand_total += final_cost
                results.append(
                    {
                        "service": service.upper(),
                        "status": "ok",
                        "source": "past projects (inflated)",
                        "estimated_cost": final_cost,
                    }
                )

    # 5. Summary Report
    print("\n" + "=" * 65)
    print(
        f"MEP COST ESTIMATION SUMMARY | {building_type.upper()} | "
        f"{area_sqm:,.2f} m2 | {height_m:g} m | Target: {year_construction}"
    )
    print("=" * 65)
    for row in results:
        if row["status"] == "not applicable":
            print(f"{row['service']:<10}: not applicable")
        elif row["status"] == "error":
            print(f"{row['service']:<10}: error, previous data not found")
        else:
            print(
                f"{row['service']:<10}: {CURRENCY} {row['estimated_cost']:>14,.2f}  [{row['source']}]"
            )
    print("-" * 65)
    print(f"{'TOTAL':<10}: {CURRENCY} {grand_total:>14,.2f}")
    print("=" * 65)


if __name__ == "__main__":
    run_mep_estimator()


