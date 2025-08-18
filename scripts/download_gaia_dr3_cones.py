#!/usr/bin/env python
"""
Download Gaia DR3 cones (one per Nickel pointing) using astroquery.gaia.

Requirements:
    pip install astroquery pandas
"""

from pathlib import Path
import time
import numpy as np
import pandas as pd
from astroquery.gaia import Gaia

# ---------------------- User Parameters ----------------------
RADIUS_DEG = 1.1  # Nickel frame edge is 6 arcmin, so cone radius is (R*sqrt(2)/2) plus ~ 1arc min margin for error
OUTDIR = Path("./data/gaia_dr3_cones")
OUTDIR.mkdir(parents=True, exist_ok=True)
MAX_ROWS = None  # or an int like 200000
MAX_RETRIES = 3
SLEEP_BETWEEN = 2.0  # seconds between TAP queries
# -------------------------------------------------------------

Gaia.MAIN_GAIA_TABLE = "gaiadr3.gaia_source"
Gaia.TIMEOUT = 600  # seconds

def run_one_cone(ra, dec, radius_deg, idx, overwrite=False):
    """Run a single cone search and save results as a CSV if not already done."""
    outpath = OUTDIR / f"gaia_dr3_cone_{idx:04d}_{ra:.5f}_{dec:.5f}.csv"
    if outpath.exists() and not overwrite:
        print(f"[{idx}] Skipping existing file: {outpath.name}")
        return outpath

    query = f"""
        SELECT *
        FROM gaiadr3.gaia_source
        WHERE CONTAINS(
            POINT('ICRS', ra, dec),
            CIRCLE('ICRS', {ra}, {dec}, {radius_deg})
        ) = 1
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            job = Gaia.launch_job_async(
                query,
                dump_to_file=False,
                output_format="votable"
            )
            results = job.get_results()
            if MAX_ROWS is not None and len(results) > MAX_ROWS:
                results = results[:MAX_ROWS]

            df = results.to_pandas()
            if df.empty or "source_id" not in df.columns.str.lower():
                print(f"[{idx}] Warning: Empty or missing source_id for RA={ra}, Dec={dec}")
                return None

            df.to_csv(outpath, index=False)
            return outpath
        except Exception as e:
            print(f"[{idx}] Attempt {attempt}/{MAX_RETRIES} failed for RA={ra}, Dec={dec}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)
            else:
                return None

def main(ras, decs, radius_deg=RADIUS_DEG, merge_after=True, download_only=False, overwrite=False):
    if download_only:
        outputs = sorted(OUTDIR.glob("gaia_dr3_cone_*.csv"))
        print(f"Found {len(outputs)} existing CSVs in {OUTDIR}")
        return merge_outputs(outputs, merge_after)

    if len(ras) != len(decs):
        raise ValueError(f"RA and Dec arrays must be the same length. Got {len(ras)} vs {len(decs)}")

    ras = np.asarray(ras, dtype=float)
    decs = np.asarray(decs, dtype=float)

    uniq, inv = np.unique(np.round(np.column_stack([ras, decs]), 6), axis=0, return_inverse=True)
    ras_u, decs_u = uniq[:, 0], uniq[:, 1]

    print(f"Total pointings: {len(ras)}; unique: {len(ras_u)}")
    outputs = []

    for i, (ra, dec) in enumerate(zip(ras_u, decs_u), start=1):
        print(f"--> [{i}/{len(ras_u)}] RA={ra:.6f}, Dec={dec:.6f}")
        out = run_one_cone(ra, dec, radius_deg, i, overwrite=overwrite)
        if out is not None:
            outputs.append(out)
        time.sleep(SLEEP_BETWEEN)

    merge_outputs(outputs, merge_after)
    print("Done.")


def merge_outputs(outputs, merge_after):
    if not merge_after or not outputs:
        return

    dfs = []
    for p in outputs:
        try:
            df = pd.read_csv(p)
            df.columns = df.columns.str.lower()
            if "source_id" not in df.columns:
                print(f"Warning: 'source_id' missing in {p}. Skipping.")
            else:
                dfs.append(df)
        except Exception as e:
            print(f"Could not read {p}: {e}")

    if not dfs:
        print("No valid files to merge.")
        return

    big = pd.concat(dfs, ignore_index=True).drop_duplicates(subset="source_id")
    out_csv = OUTDIR.parent / "gaia_dr3_all_cones" / "gaia_dr3_all_cones.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    big.to_csv(out_csv, index=False)
    print(f"Saved merged CSV: {out_csv} (rows={len(big)})")

if __name__ == "__main__":
    ras = np.array([210.49770143, 210.66507643, 210.85303477, 211.02045143,
       211.22995143, 211.52265977, 211.73170143, 211.94120143,
       212.19178477, 212.4218681 , 212.61032643, 212.96615977,
       213.23782644, 213.42628477, 213.6563681 , 213.90745144,
       216.22961811, 216.43865977, 216.56395144, 216.71078477,
       216.83611811, 216.96190978, 217.10828478, 217.23357644,
       217.37995144, 217.50574311, 218.66999701, 218.66995534,
       218.66358034, 217.76453845, 217.76458012, 179.11704318,
       233.29578582, 233.29586916, 233.29582749, 233.29566082,
       233.29591082, 233.29586916, 233.29566082, 233.29582749,
       266.33328528, 266.33328528, 266.33332695, 266.18328528,
       266.33336862, 266.33324362, 266.33336862, 266.33328528,
       266.33332695, 266.33341028, 266.33324362, 266.33332695,
       277.91749456, 277.91749456, 277.91749456, 266.33324362,
       266.33332695, 266.33332695, 266.33620195, 266.33607695,
       277.91757789])
    decs = np.array([-5.09816729, -5.09836176, -5.098584  , -5.09880624, -5.09905626,
       -5.09938962, -5.09966742, -5.09991745, -5.10022303, -5.10050083,
       -5.10075085, -5.10119533, -5.10152869, -5.10177871, -5.10208429,
       -5.10241765, -5.10552901, -5.10580681, -5.10597348, -5.10619572,
       -5.1063624 , -5.10652908, -5.10672354, -5.106918  , -5.10711246,
       -5.10727914, 29.74499852, 29.72830408, 29.73110963, 28.00972084,
       28.02916528, 55.12528039,  5.53949708,  5.53941375,  5.53944152,
        5.5394693 ,  5.53944152,  5.53949708,  5.53938597,  5.53949708,
       -0.4369498 , -0.43683869, -0.4369498 , -0.53694979, -0.43692202,
       -0.43692202, -0.43692202, -0.43697758, -0.43706091, -0.43681091,
       -0.43683869, -0.43697758, 27.87693862, 27.92138307, 27.92138307,
       -0.43697758, -0.43697758, -0.4369498 , -0.43689424, -0.43417202,
       27.90054973])
    
    main(ras, decs, merge_after=True, download_only=False, overwrite=False)

