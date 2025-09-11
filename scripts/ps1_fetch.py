#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Batch-fetch PS1 DR2 cones (MeanObjectView) for LSST refcats.

Inputs (choose one):
  --fits-dir PATH        # scan FITS, use WCS/headers for RA/Dec
  --csv PATH             # CSV with columns: ra,dec (degrees)
  --butler REPO          # Butler repo, read visit.region centroids
  --ras/--decs           # comma-separated arrays of degrees

Requires:
  pip install mastcasjobs astropy pandas pyarrow python-dotenv

CasJobs auth:
  Put CASJOBS_USERID=... and CASJOBS_PW=... in a .env next to this script,
  or export them in your shell env.

Output:
  Parquet shards per batch + merged Parquet/CSV:
    ./data/ps1_all_cones/merged_ps1_cones.parquet
    ./data/ps1_all_cones/merged_ps1_cones.csv
"""

from __future__ import annotations

import argparse
import math
import os
import time
from pathlib import Path
from typing import Iterable, Tuple

import numpy as np
import pandas as pd
from dotenv import load_dotenv

# --- External deps (install if missing) ---
# pip install mastcasjobs
import mastcasjobs

# ------------ Defaults ------------
BATCH_SIZE_DEFAULT = 50            # cones per CasJobs VALUES block
RADIUS_ARCMIN_DEFAULT = 5.4        # ~0.09 deg; Nickel ~6' field + margin
SLEEP_BETWEEN_DEFAULT = 2.0        # seconds between CasJobs polls
MAX_RETRIES_DEFAULT = 3
CONTEXT = "PanSTARRS_DR2"          # CasJobs context
# Columns needed by your convert config (plus useful extras)
COLS_SQL = """
  m.objID, m.raMean, m.decMean, m.raMeanErr, m.decMeanErr,
  m.epochMean,
  m.gMeanPSFMag, m.rMeanPSFMag, m.iMeanPSFMag, m.zMeanPSFMag, m.yMeanPSFMag,
  m.gMeanPSFMagErr, m.rMeanPSFMagErr, m.iMeanPSFMagErr, m.zMeanPSFMagErr, m.yMeanPSFMagErr,
  m.nDetections, m.ng, m.nr, m.ni, m.nz, m.ny,
  m.qualityFlag, m.objInfoFlag
""".strip()
# ----------------------------------


# ===========================
# Shared pointing helpers (Butler/FITS/CSV/arrays)
# ===========================

def uniq_pairs(ras: np.ndarray, decs: np.ndarray, round_ndp: int = 6) -> Tuple[np.ndarray, np.ndarray]:
    arr = np.column_stack([ras, decs])
    uniq, _ = np.unique(np.round(arr, round_ndp), axis=0, return_inverse=True)
    return uniq[:, 0], uniq[:, 1]


def _unitvec_to_xyz(u) -> Tuple[float, float, float]:
    x = y = z = None
    if hasattr(u, "getX"):
        x, y, z = u.getX(), u.getY(), u.getZ()
    else:
        x = getattr(u, "x", None); x = x() if callable(x) else x
        y = getattr(u, "y", None); y = y() if callable(y) else y
        z = getattr(u, "z", None); z = z() if callable(z) else z
    return float(x), float(y), float(z)


def _region_centroid_radec(region) -> Tuple[float, float]:
    if hasattr(region, "getVertices"):
        verts = list(region.getVertices())
    elif hasattr(region, "getVerticesIter"):
        verts = list(region.getVerticesIter())
    else:
        raise RuntimeError("ConvexPolygon region has no getVertices*()")
    if not verts:
        raise RuntimeError("ConvexPolygon has no vertices")
    xyz = np.array([_unitvec_to_xyz(v) for v in verts], dtype=float)
    m = xyz.mean(axis=0); m /= np.linalg.norm(m)
    x, y, z = m
    ra = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    dec = math.degrees(math.asin(z))
    return ra, dec


def _pointings_from_visit_regions(butler, instrument: str, include_calibs: bool, registry_where: str | None):
    base_where = "instrument = @inst"
    if registry_where:
        base_where = f"{base_where} AND ({registry_where})"
    recs = butler.registry.queryDimensionRecords("visit", where=base_where, bind={"inst": instrument})
    for v in recs:
        if not include_calibs:
            if getattr(v, "observation_reason", None) == "calibration":
                continue
            tn = (getattr(v, "target_name", "") or "").lower()
            if any(k in tn for k in ("flat", "bias", "dark")):
                continue
        region = getattr(v, "region", None)
        if region is None:
            continue
        try:
            yield _region_centroid_radec(region)
        except Exception:
            continue


def _maybe_load_pointings_from_butler(
    repo: str,
    instrument: str = "Nickel",
    include_calibs: bool = False,
    registry_where: str | None = None,
) -> Iterable[Tuple[float, float]]:
    from lsst.daf.butler import Butler
    b = Butler(repo)
    got = False
    for tup in _pointings_from_visit_regions(b, instrument=instrument,
                                             include_calibs=include_calibs,
                                             registry_where=registry_where):
        got = True
        yield tup
    if not got:
        raise RuntimeError("No visit.region pointings found in Butler registry. "
                           "Try --include-calibs or adjust --registry-where.")


def _fits_paths(root: str | Path) -> Iterable[Path]:
    root = Path(root)
    exts = (".fits", ".fit", ".fz", ".fits.fz")
    for p in sorted(root.rglob("*")):
        low = str(p).lower()
        if p.suffix.lower() in exts or any(low.endswith(e) for e in exts):
            yield p


def _parse_ra_dec_from_header(hdr) -> Tuple[float, float]:
    from astropy.wcs import WCS
    from astropy.coordinates import SkyCoord
    import astropy.units as u
    try:
        w = WCS(hdr)
        nx = int(hdr.get("NAXIS1", 0)); ny = int(hdr.get("NAXIS2", 0))
        if nx > 0 and ny > 0 and w.has_celestial:
            sky = w.pixel_to_world(nx/2.0, ny/2.0)
            return float(sky.ra.deg), float(sky.dec.deg)
    except Exception:
        pass
    if "CRVAL1" in hdr and "CRVAL2" in hdr:
        try:
            return float(hdr["CRVAL1"]), float(hdr["CRVAL2"])
        except Exception:
            pass
    for rkey, dkey in [("OBJCTRA", "OBJCTDEC"), ("RA", "DEC")]:
        if rkey in hdr and dkey in hdr:
            try:
                sc = SkyCoord(hdr[rkey], hdr[dkey], unit=(u.hourangle, u.deg))
                return float(sc.ra.deg), float(sc.dec.deg)
            except Exception:
                try:
                    sc = SkyCoord(hdr[rkey], hdr[dkey], unit=(u.deg, u.deg))
                    return float(sc.ra.deg), float(sc.dec.deg)
                except Exception:
                    pass
    raise RuntimeError("No usable WCS/RA/DEC found in FITS header")


def pointings_from_fits_dir(fits_dir: str | Path) -> Iterable[Tuple[float, float]]:
    from astropy.io import fits
    for p in _fits_paths(fits_dir):
        try:
            with fits.open(p, memmap=True) as hdul:
                hdr = None
                for hdu in hdul:
                    if getattr(hdu, "data", None) is not None:
                        hdr = hdu.header; break
                if hdr is None:
                    hdr = hdul[0].header
                yield _parse_ra_dec_from_header(hdr)
        except Exception:
            continue


def load_pointings(args) -> Tuple[np.ndarray, np.ndarray]:
    if args.fits_dir:
        ras, decs = zip(*pointings_from_fits_dir(args.fits_dir))
        return np.array(ras, float), np.array(decs, float)
    if args.csv and Path(args.csv).exists():
        df = pd.read_csv(args.csv)
        return df["ra"].to_numpy(float), df["dec"].to_numpy(float)
    if args.butler:
        ras, decs = zip(*_maybe_load_pointings_from_butler(
            args.butler, instrument=args.instrument,
            include_calibs=args.include_calibs,
            registry_where=args.registry_where))
        return np.array(ras, float), np.array(decs, float)
    if args.ras and args.decs:
        ras = np.array([float(x) for x in args.ras.split(",")], float)
        decs = np.array([float(x) for x in args.decs.split(",")], float)
        return ras, decs
    raise SystemExit("No pointings provided. Use --fits-dir, --csv, --butler, or --ras/--decs.")


# ===========================
# CasJobs batch query
# ===========================

def _casjobs_login(context: str = CONTEXT) -> mastcasjobs.MastCasJobs:
    load_dotenv(Path(".env"))
    user = os.environ.get("CASJOBS_USERID")
    pwd = os.environ.get("CASJOBS_PW")
    if not user or not pwd:
        raise SystemExit("CASJOBS_USERID / CASJOBS_PW not set (env or .env).")
    return mastcasjobs.MastCasJobs(username=user, password=pwd, context=context)


def _values_block(ras: np.ndarray, decs: np.ndarray, r_arcmin: float) -> str:
    # Build a T-SQL VALUES table with (ra, dec, rad)
    rows = ",\n    ".join(f"({ra:.8f}, {dec:.8f}, {r_arcmin:.6f})" for ra, dec in zip(ras, decs))
    return f"(VALUES\n    {rows}\n) AS cones(ra, dec, rad)"


def _submit_batch(cas: mastcasjobs.MastCasJobs, ras: np.ndarray, decs: np.ndarray,
                  r_arcmin: float, batch_idx: int, table_prefix: str = "ps1_batch") -> Tuple[str, str]:
    """
    Submit one CasJobs job that selects from a VALUES block of cones via CROSS APPLY.
    Returns (job_id, out_table_name).
    """
    out_table = f"{table_prefix}_{batch_idx:04d}_{int(time.time())}"
    values = _values_block(ras, decs, r_arcmin)
    # Use SELECT INTO MYDB.<table> so we can fast_table it afterwards.
    query = f"""
    SELECT {COLS_SQL}
    INTO MYDB.{out_table}
    FROM {values}
    CROSS APPLY fGetNearbyObjEq(cones.ra, cones.dec, cones.rad) AS nb
    JOIN MeanObjectView AS m ON nb.objID = m.objID
    """
    job_id = cas.submit(query, task_name=f"submit_{out_table}")
    return job_id, out_table


def _wait_job(cas: mastcasjobs.MastCasJobs, job_id: str, sleep: float, max_retries: int) -> None:
    nerr = 0
    while True:
        try:
            num, status = cas.status(job_id)
            status = (status or "").lower()
            if status == "finished":
                return
            if status in {"failed", "cancelled"}:
                raise RuntimeError(f"CasJobs job {job_id} ended: {status}")
            time.sleep(sleep)
        except Exception:
            nerr += 1
            if nerr > max_retries:
                raise
            time.sleep(sleep * nerr)


def _drop_table(cas: mastcasjobs.MastCasJobs, name: str) -> None:
    # Be tolerant across library versions
    try:
        cas.drop_table_if_exists(name)
    except Exception:
        try:
            cas.quick(f"DROP TABLE MYDB.{name}")
        except Exception:
            pass


# ===========================
# Main
# ===========================

def main():
    ap = argparse.ArgumentParser(description="Batch PS1 DR2 cones (MeanObjectView) for LSST refcats.")
    # Inputs
    ap.add_argument("--fits-dir", default=None, help="Directory of FITS files to read pointings from")
    ap.add_argument("--csv", default=None, help="CSV with columns ra,dec (degrees)")
    ap.add_argument("--butler", default=None, help="Butler repo to read visit.region pointings")
    ap.add_argument("--instrument", default="Nickel", help="Instrument name in the Butler repo (default: Nickel)")
    ap.add_argument("--registry-where", default=None,
                    help="Extra WHERE for registry, e.g. \"physical_filter = 'I' AND day_obs>=20240601\"")
    ap.add_argument("--include-calibs", action="store_true",
                    help="Include calibration visits when reading from Butler")
    # Arrays
    ap.add_argument("--ras", default=None, help="Comma-separated RAs in degrees")
    ap.add_argument("--decs", default=None, help="Comma-separated Decs in degrees")
    # Query/IO
    ap.add_argument("--radius-arcmin", type=float, default=RADIUS_ARCMIN_DEFAULT, help="Cone radius in arcmin")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE_DEFAULT, help="Cones per batch")
    ap.add_argument("--sleep", type=float, default=SLEEP_BETWEEN_DEFAULT, help="Poll interval (s)")
    ap.add_argument("--max-retries", type=int, default=MAX_RETRIES_DEFAULT, help="Max transient poll errors")
    ap.add_argument("--outdir", default="./data/ps1_cones_batched", help="Directory for per-batch CSVs")
    ap.add_argument("--merged-parquet", default="./data/ps1_all_cones/merged_ps1_cones.parquet",
                    help="Path for merged Parquet")
    ap.add_argument("--merged-csv", default="./data/ps1_all_cones/merged_ps1_cones.csv",
                    help="Path for merged CSV")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing batch files")

    args = ap.parse_args()

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    Path(args.merged_parquet).parent.mkdir(parents=True, exist_ok=True)
    Path(args.merged_csv).parent.mkdir(parents=True, exist_ok=True)

    # Resolve pointings
    ras, decs = load_pointings(args)
    ras, decs = uniq_pairs(ras, decs)
    print(f"Unique pointings: {len(ras)} | radius={args.radius_arcmin:.2f} arcmin | batch={args.batch_size}")

    # CasJobs login
    cas = _casjobs_login(context=CONTEXT)

    # Batch loop
    shards: list[Path] = []
    n = len(ras)
    for k, i0 in enumerate(range(0, n, args.batch_size), start=1):
        i1 = min(i0 + args.batch_size, n)
        rr, dd = ras[i0:i1], decs[i0:i1]

        out_table = None
        outfile = outdir / f"ps1_batch_{k:04d}.csv"
        if outfile.exists() and not args.overwrite:
            print(f"[{k}] SKIP existing shard: {outfile.name}")
            shards.append(outfile)
            continue

        try:
            # Submit and wait
            job_id, out_table = _submit_batch(cas, rr, dd, args.radius_arcmin, k)
            print(f"[{k}] Submitted job {job_id} → table MYDB.{out_table}")
            _wait_job(cas, job_id, args.sleep, args.max_retries)

            # Download
            table = cas.fast_table(table=out_table)
            df = table.to_pandas()
            # Normalize column names to exactly what your config expects
            # (MeanObjectView already uses these names)
            df.to_csv(outfile, index=False)
            shards.append(outfile)
            print(f"[{k}] rows={len(df)} → {outfile.name}")
        finally:
            if out_table:
                _drop_table(cas, out_table)

    if not shards:
        raise SystemExit("No shards created. Nothing to merge.")

    # Merge + dedup by objID
    frames = [pd.read_csv(p) for p in shards]
    merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset="objID")

    # Sanity columns
    need = {
        "objID", "raMean", "decMean", "raMeanErr", "decMeanErr", "epochMean",
        "gMeanPSFMag", "rMeanPSFMag", "iMeanPSFMag", "zMeanPSFMag", "yMeanPSFMag",
        "gMeanPSFMagErr", "rMeanPSFMagErr", "iMeanPSFMagErr", "zMeanPSFMagErr", "yMeanPSFMagErr",
        "nDetections", "ng", "nr", "ni", "nz", "ny", "qualityFlag", "objInfoFlag",
    }
    missing = need - set(merged.columns)
    if missing:
        raise SystemExit(f"Missing expected columns in PS1 merge: {sorted(missing)}")

    # Write outputs
    merged.to_parquet(args.merged_parquet, index=False)
    merged.to_csv(args.merged_csv, index=False)

    print(f"\nSaved merged Parquet: {args.merged_parquet}  (rows={len(merged)})")
    print(f"Saved merged CSV:     {args.merged_csv}      (rows={len(merged)})")
    print("Done.")


if __name__ == "__main__":
    main()
