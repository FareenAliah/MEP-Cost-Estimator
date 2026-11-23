MEP COST ESTIMATOR 

What it does:
  1. (Optional) reads area callouts such as "GFA: 12,500 m2" from a PDF drawing
  2. Asks for building details and which MEP systems are needed
  3. Estimates cost from your historical CSV, or from market benchmarks
  4. Adds inflation + safety factor and prints a report

NOTE: Rates below are ILLUSTRATIVE PLACEHOLDERS. Replace with your own
supplier / subcontractor rates before relying on any result.

Install once:  pip install pymupdf pandas
"""
import os
import re

import pandas as pd

# =====================================================================
# SETTINGS (change here, not inside the code below)
# =====================================================================
INFLATION_RATE = 0.06      # 6% - set to 0 if your rates are already current
SAFETY_FACTOR = 0.10       # 10%
HEIGHT_ADJ_PER_M = 0.01    # 1% cost change per metre of height difference
CURRENCY = "MYR"

# =====================================================================
# MODULE 1: PDF AREA EXTRACTION
# =====================================================================
# Matches e.g. "GFA: 12,500.50 m2", "Area = 4500 sqm", "GFA (m²): 1200"
AREA_PATTERN = re.compile(
    r"(?:GFA|GROSS FLOOR AREA|TOTAL AREA|AREA)"
    r"\s*(?:\([^)]*\))?"           # optional "(m2)" after the label
    r"\s*[:=\-]?\s*"
    r"([\d,]+\.?\d*)"              # the number
    r"\s*(?:m2|m²|sqm|sq\.?\s?m)?",  # the unit (optional if given in brackets)
    re.IGNORECASE,
)


def extract_area_from_pdf(pdf_path):
    """Return a list of (page_number, area_m2) found in the PDF text."""
    if not os.path.exists(pdf_path):
        print(f"[Error] File not found: {pdf_path}")
        return []

    try:
        import fitz  # PyMuPDF (imported here so the program still runs without it)
    except ImportError:
        print("[Error] PyMuPDF not installed. Run: pip install pymupdf")
        return []

    found = []
    doc = fitz.open(pdf_path)
    try:
        for page_num in range(len(doc)):
            text = doc[page_num].get_text("text")
            for match in AREA_PATTERN.findall(text):
                try:
                    value = float(match.replace(",", "").rstrip("."))
                except ValueError:
                    continue
                if value > 0:
                    found.append((page_num + 1, value))
    finally:
        doc.close()  # always release the file

    if not found:
        print("[Note] No text found. A scanned drawing needs OCR instead.")
    return found


# =====================================================================
# MODULE 2: MARKET BENCHMARK DATABASE (fallback when no CSV)
# =====================================================================
MARKET_BENCHMARKS = {
    "acmv": {
        "vrv system": {"base_rate_m2": 320.0, "height_factor_per_m": 0.005},
        "split type": {"base_rate_m2": 180.0, "height_factor_per_m": 0.002},
        "chilled water": {"base_rate_m2": 450.0, "height_factor_per_m": 0.008},
    },
    "plumbing": {
        "standard cold & hot water": {"base_rate_m2": 85.0, "height_factor_per_m": 0.004},
        "cold water only": {"base_rate_m2": 55.0, "height_factor_per_m": 0.003},
    },
    "fire": {
        "sprinkler + hydrant + alarm": {"base_rate_m2": 110.0, "height_factor_per_m": 0.005},
        "hose reel + alarm only": {"base_rate_m2": 45.0, "height_factor_per_m": 0.002},
    },
    "lift": {
        "high-speed passenger lift": {"base_rate_m2": 95.0, "height_factor_per_m": 0.012},
        "standard passenger/goods lift": {"base_rate_m2": 65.0, "height_factor_per_m": 0.008},
    },
}

BUILDING_TYPE_MULTIPLIERS = {
    "hotel": 1.25,
    "office": 1.10,
    "residential high rise": 1.05,
    "residential landed": 0.90,
    "warehouses": 1.25,
}


# =====================================================================
# MODULE 3: INPUT HELPERS (stop crashes from bad typing)
# =====================================================================
def ask_float(prompt, default=None):
    """Keep asking until the user types a positive number."""
    while True:
        text = input(prompt).strip().replace(",", "")
        if not text and default is not None:
            return default
        try:
            value = float(text)
            if value > 0:
                return value
        except ValueError:
            pass
        print("  Please enter a number greater than 0.")


def ask_choice(prompt, options):
    """Keep asking until the user types one of the options."""
    while True:
        text = input(prompt).strip().lower()
        if text in options:
            return text
        print(f"  Please choose one of: {', '.join(options)}")


def resolve_spec(service, typed_spec):
    """
    Match what the user typed to a known benchmark name.
    If nothing matches, show the options and let the user pick
    (instead of silently using the wrong one).
    """
    options = list(MARKET_BENCHMARKS[service.lower()].keys())
    typed = typed_spec.lower().strip()

    for name in options:
        if typed == name or typed in name or name in typed:
            return name

    print(f"\n  '{typed_spec}' is not a known {service} system. Options:")
    for i, name in enumerate(options, start=1):
        print(f"    {i}. {name}")
    while True:
        pick = input("  Pick a number: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(options):
            return options[int(pick) - 1]
        print("  Invalid choice.")


# =====================================================================
# MODULE 4: HISTORICAL DATA LOADER & COST ENGINE
# =====================================================================
REQUIRED_COLUMNS = ["service", "building_type", "system_spec",
                    "area_m2", "height_m", "awarded_cost"]


def load_historical_data(csv_path):
    """
    CSV columns needed:
    service, building_type, system_spec, area_m2, height_m, awarded_cost
    Returns a cleaned DataFrame, or None.
    """
    if not csv_path or not os.path.exists(csv_path):
        return None
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"[Warning] Could not read historical file: {e}")
        return None

    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        print(f"[Warning] CSV is missing columns: {missing}. Ignoring file.")
        return None

    # Make numbers numeric, drop bad rows (blank, text, zero area)
    for col in ["area_m2", "height_m", "awarded_cost"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=REQUIRED_COLUMNS)
    df = df[(df["area_m2"] > 0) & (df["awarded_cost"] > 0)]

    # Clean text columns once, so matching is easy later
    for col in ["service", "building_type", "system_spec"]:
        df[col] = df[col].astype(str).str.lower().str.strip()

    if df.empty:
        print("[Warning] No valid rows left in historical file.")
        return None
    return df


def estimate_service_cost(service, system_spec, area_m2, height_m,
                          building_type, hist_df):
    """
    Returns (base_cost, source_description).
    Tries historical data first, then market benchmarks.
    """
    service_key = service.lower().strip()
    spec_key = system_spec.lower().strip()
    bldg_key = building_type.lower().strip()

    # ---- 1. Historical match ----
    if hist_df is not None:
        matches = hist_df[hist_df["service"] == service_key]
        # regex=False so symbols like "+" are treated as normal text
        matches = matches[matches["system_spec"].str.contains(spec_key, regex=False)]

        if not matches.empty:
            # Prefer projects of the same building type, if any exist
            same_type = matches[matches["building_type"] == bldg_key]
            note = "any building type"
            if not same_type.empty:
                matches = same_type
                note = "same building type"

            matches = matches.copy()
            matches["rate_per_m2"] = matches["awarded_cost"] / matches["area_m2"]
            avg_rate = matches["rate_per_m2"].mean()
            avg_height = matches["height_m"].mean()

            height_adj = max(0.8, 1.0 + (height_m - avg_height) * HEIGHT_ADJ_PER_M)
            base_cost = avg_rate * area_m2 * height_adj
            source = f"Historical Data ({len(matches)} record(s), {note})"
            return base_cost, source

    # ---- 2. Market benchmark fallback ----
    benchmarks = MARKET_BENCHMARKS[service_key]
    rates = benchmarks[spec_key]  # spec_key was validated by resolve_spec()

    bldg_mult = BUILDING_TYPE_MULTIPLIERS[bldg_key]
    height_mult = 1.0 + height_m * rates["height_factor_per_m"]
    base_cost = rates["base_rate_m2"] * area_m2 * bldg_mult * height_mult
    return base_cost, "Market Benchmark Rate (supplier + labour)"


# =====================================================================
# MODULE 5: MAIN WORKFLOW & REPORT
# =====================================================================
def run_mep_estimator():
    print("=== MEP COST ESTIMATION SYSTEM ===\n")

    # ---- Step 1: PDF area (optional) ----
    extracted_area = None
    pdf_path = input("Enter path to PDF drawing (or press Enter to skip): ").strip().strip('"')
    if pdf_path:
        areas = extract_area_from_pdf(pdf_path)
        if areas:
            print("\nDetected area callouts in PDF:")
            for i, (page, val) in enumerate(areas, start=1):
                print(f"  {i}. Page {page}: {val:,.2f} m2")
            while True:
                pick = input("Which one is the TOTAL building area? "
                             "(number, or Enter to type manually): ").strip()
                if not pick:
                    break
                if pick.isdigit() and 1 <= int(pick) <= len(areas):
                    extracted_area = areas[int(pick) - 1][1]
                    break
                print("  Invalid choice.")

    # ---- Step 2: Building inputs ----
    if extracted_area:
        total_area = ask_float(f"1. Overall building area in m2 [{extracted_area:,.2f}]: ",
                               default=extracted_area)
    else:
        total_area = ask_float("1. Overall building area in m2: ")

    height_m = ask_float("2. Height of the building (in metres): ")

    types = list(BUILDING_TYPE_MULTIPLIERS.keys())
    print("\nBuilding types: " + " | ".join(types))
    building_type = ask_choice("3. Type of building: ", types)

    # ---- Step 3: Systems ----
    print("\n4. Select required MEP systems (press Enter to skip if NOT applicable).")
    prompts = {
        "ACMV": "   - ACMV (VRV system / Split type / Chilled water): ",
        "Plumbing": "   - Plumbing (Standard cold & hot water / Cold water only): ",
        "Fire": "   - Fire (Sprinkler + hydrant + alarm / Hose reel + alarm only): ",
        "Lift": "   - Lift (High-speed passenger lift / Standard passenger/goods lift): ",
    }
    required_systems = {}
    for service, prompt in prompts.items():
        typed = input(prompt).strip()
        if typed:
            required_systems[service] = resolve_spec(service, typed)

    if not required_systems:
        print("\nNo systems selected. Nothing to estimate.")
        return

    # ---- Step 4: Historical CSV ----
    hist_path = input("\n5. Path to previous awarded-cost CSV (or press Enter if none): ").strip().strip('"')
    hist_df = load_historical_data(hist_path)
    if hist_path and hist_df is None:
        print("   (Continuing with market benchmarks.)")

    # ---- Step 5: Report ----
    print("\n" + "=" * 75)
    print(f"MEP COST ESTIMATION REPORT | {building_type.upper()} | "
          f"{total_area:,.2f} m2 | {height_m:g} m height")
    print("=" * 75)

    grand_total = 0.0
    for service in ["ACMV", "Plumbing", "Fire", "Lift"]:
        if service not in required_systems:
            print(f"\n[{service.upper()}] -> NOT APPLICABLE (no costing)")
            continue

        spec = required_systems[service]
        base_cost, source = estimate_service_cost(
            service, spec, total_area, height_m, building_type, hist_df
        )

        inflation_amt = base_cost * INFLATION_RATE
        subtotal = base_cost + inflation_amt
        safety_amt = subtotal * SAFETY_FACTOR
        final_cost = subtotal + safety_amt
        grand_total += final_cost

        print(f"\n[{service.upper()}] - Spec: {spec}")
        print(f"  Data Source           : {source}")
        print(f"  Base Cost             : {CURRENCY} {base_cost:,.2f}")
        print(f"  + Inflation ({INFLATION_RATE:.0%})       : {CURRENCY} {inflation_amt:,.2f}")
        print(f"  + Safety Factor ({SAFETY_FACTOR:.0%})  : {CURRENCY} {safety_amt:,.2f}")
        print(f"  Total Estimation      : {CURRENCY} {final_cost:,.2f}")
        print(f"  Cost per Area         : {final_cost / total_area:,.2f} / m2")
        print(f"  Cost per Height       : {final_cost / height_m:,.2f} / m")

    print("\n" + "=" * 75)
    print(f"TOTAL ESTIMATED MEP COST: {CURRENCY} {grand_total:,.2f}")
    print(f"OVERALL COST PER AREA   : {CURRENCY} {grand_total / total_area:,.2f} / m2")
    print("=" * 75)
    print("Note: early-stage estimate only. Verify rates with current quotations.")


if __name__ == "__main__":
    run_mep_estimator()
