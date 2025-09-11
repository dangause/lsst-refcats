#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Batch-fetch Gaia DR3 cones for LSST refcats (Nickel-friendly).

Inputs (choose one):
  1) --fits-dir PATH        # scan FITS, use WCS/headers to get RA/Dec
  2) --csv PATH             # CSV with columns: ra, dec (degrees)
  3) --butler REPO          # Butler repo, read visit.region centroids
  4) --ras/--decs           # comma-separated arrays of degrees

Key features:
  - Uses a TAP-uploaded table of cone centers → UNION in a single query per batch.
  - Selects only needed Gaia DR3 columns (keeps it fast/light).
  - Writes Parquet shards per batch + a single merged Parquet and CSV.
  - Robust Butler path (no visitSummary needed): computes RA/Dec from visit.region.
  - Optional registry WHERE filter and flag to include calibration visits.

Examples:
  python scripts/gaia_fetch.py --butler /path/to/repo --instrument Nickel --radius-deg 0.09
  python scripts/gaia_fetch.py --fits-dir /path/to/raw --radius-deg 0.12
  python scripts/gaia_fetch.py --csv ./data/nickel_pointings.csv --radius-deg 0.09
"""

from __future__ import annotations

import argparse
import math
import time
from pathlib import Path
from typing import Iterable, Tuple

import numpy as np
import pandas as pd

# --- External deps (install if missing) ---
# pip install astroquery astropy pandas pyarrow
from astroquery.gaia import Gaia

Gaia.MAIN_GAIA_TABLE = "gaiadr3.gaia_source"
Gaia.TIMEOUT = 600  # seconds

# ------------ Defaults ------------
RADIUS_DEG_DEFAULT = 0.09       # ~5.4' (6' FOV w/ margin)
BATCH_SIZE_DEFAULT = 200        # cones per TAP upload batch
SLEEP_BETWEEN_DEFAULT = 2.0     # seconds between TAP jobs
MAX_RETRIES_DEFAULT = 4
# Columns required by convertReferenceCatalog ConvertGaiaManager + some errors
COLS_SQL = """
  g.source_id,
  g.ra, g.dec, g.ra_error, g.dec_error,
  g.parallax, g.parallax_error,
  g.pmra, g.pmra_error, g.pmdec, g.pmdec_error,
  g.ref_epoch,
  g.phot_g_mean_flux,  g.phot_bp_mean_flux,  g.phot_rp_mean_flux,
  g.phot_g_mean_flux_over_error, g.phot_bp_mean_flux_over_error, g.phot_rp_mean_flux_over_error,
  g.phot_g_mean_mag,   g.phot_bp_mean_mag,   g.phot_rp_mean_mag
""".strip()


# ----------------------------------


# ===========================
# Utilities
# ===========================

def uniq_pairs(ras: np.ndarray, decs: np.ndarray, round_ndp: int = 6) -> Tuple[np.ndarray, np.ndarray]:
    arr = np.column_stack([ras, decs])
    uniq, _ = np.unique(np.round(arr, round_ndp), axis=0, return_inverse=True)
    return uniq[:, 0], uniq[:, 1]


def make_batches(
    ras: np.ndarray,
    decs: np.ndarray,
    rdeg: float,
    batch_size: int,
) -> Iterable[Tuple[int, int, np.ndarray, np.ndarray, np.ndarray]]:
    n = len(ras)
    for i in range(0, n, batch_size):
        j = min(i + batch_size, n)
        yield i, j, ras[i:j], decs[i:j], np.full(j - i, rdeg, dtype=float)


def _run_one_batch(
    df_upload: pd.DataFrame,
    cols_sql: str = COLS_SQL,
    max_retries: int = MAX_RETRIES_DEFAULT,
    g_min: float = 7.0,
    g_max: float = 20.5,
) -> pd.DataFrame:
    """
    Try TAP upload via temp CSV. If the server 500s, fall back to a plain
    ADQL WHERE with an OR of CIRCLE() predicates (no uploads).
    Applies a Gaia G-band magnitude window server-side.
    """
    import os, tempfile
    from requests import HTTPError

    # Shared quality/mag clause
    quality = (
        f"g.phot_g_mean_mag BETWEEN {g_min} AND {g_max} AND "
        "g.phot_g_mean_flux_over_error > 0 AND "
        "g.phot_bp_mean_flux_over_error > 0 AND "
        "g.phot_rp_mean_flux_over_error > 0 AND "
        "g.phot_g_mean_flux IS NOT NULL AND "
        "g.phot_bp_mean_flux IS NOT NULL AND "
        "g.phot_rp_mean_flux IS NOT NULL"
    )

    upload_name = "user_cones"
    query_upload = f"""
      SELECT {cols_sql}
      FROM {Gaia.MAIN_GAIA_TABLE} AS g
      JOIN TAP_UPLOAD.{upload_name} AS uc
        ON 1 = CONTAINS(
             POINT('ICRS', g.ra, g.dec),
             CIRCLE('ICRS', uc.ra, uc.dec, uc.rdeg)
           )
      WHERE {quality}
    """

    # 1) Try upload path
    last_err = None
    for attempt in range(1, max_retries + 1):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
                df_upload.to_csv(tmp.name, index=False)
                tmp_path = tmp.name

            job = Gaia.launch_job_async(
                query=query_upload,
                upload_resource=tmp_path,
                upload_table_name=upload_name,
                dump_to_file=False,
                output_format="votable",
            )
            res = job.get_results()
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return res.to_pandas()

        except Exception as e:
            last_err = e
            if tmp_path and os.path.exists(tmp_path):
                try: os.unlink(tmp_path)
                except Exception: pass
            if isinstance(e, HTTPError) or "500" in str(e):
                break
            if attempt < max_retries:
                time.sleep(3 * attempt)
            else:
                break  # fall through to fallback

    # 2) Fallback: OR-of-CIRCLE with same quality clause
    circles = [
        f"CONTAINS(POINT('ICRS', g.ra, g.dec), CIRCLE('ICRS', {ra:.8f}, {dec:.8f}, {r:.6f}))=1"
        for ra, dec, r in zip(df_upload['ra'].to_numpy(float),
                              df_upload['dec'].to_numpy(float),
                              df_upload['rdeg'].to_numpy(float))
    ]
    chunk_size = 50
    frames = []
    for k in range(0, len(circles), chunk_size):
        where = "(" + " OR ".join(circles[k:k+chunk_size]) + f") AND ({quality})"
        query_plain = f"SELECT {cols_sql} FROM {Gaia.MAIN_GAIA_TABLE} AS g WHERE {where}"
        got = None
        for attempt in range(1, max_retries + 1):
            try:
                job = Gaia.launch_job_async(
                    query=query_plain,
                    dump_to_file=False,
                    output_format="votable",
                )
                got = job.get_results().to_pandas()
                break
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(3 * attempt)
                else:
                    raise RuntimeError(f"Plain ADQL fallback failed after {max_retries} attempts: {e}")
        got.columns = [c.lower() for c in got.columns]
        frames.append(got)

    if not frames:
        raise RuntimeError(f"TAP upload failed with: {last_err}")

    return pd.concat(frames, ignore_index=True)





# ===========================
# Butler helpers (no visitSummary required)
# ===========================

def _unitvec_to_xyz(u) -> Tuple[float, float, float]:
    """Extract (x, y, z) from lsst.sphgeom.UnitVector3d (supports getX/getY/getZ or attributes)."""
    x = y = z = None
    if hasattr(u, "getX"):
        x = u.getX()
        y = u.getY()
        z = u.getZ()
    else:
        # attribute style
        x = getattr(u, "x", None)
        y = getattr(u, "y", None)
        z = getattr(u, "z", None)
        # some bindings expose callables
        x = x() if callable(x) else x
        y = y() if callable(y) else y
        z = z() if callable(z) else z
    return float(x), float(y), float(z)


def _region_centroid_radec(region) -> Tuple[float, float]:
    """
    Compute (ra_deg, dec_deg) from a ConvexPolygon by averaging vertex unit vectors.
    """
    # vertices() can show up under getVertices() or getVerticesIter()
    if hasattr(region, "getVertices"):
        verts = list(region.getVertices())
    elif hasattr(region, "getVerticesIter"):
        verts = list(region.getVerticesIter())
    else:
        raise RuntimeError("ConvexPolygon region has no getVertices*()")
    if not verts:
        raise RuntimeError("ConvexPolygon has no vertices")

    xyz = np.array([_unitvec_to_xyz(v) for v in verts], dtype=float)
    m = xyz.mean(axis=0)
    norm = np.linalg.norm(m)
    if not np.isfinite(norm) or norm == 0.0:
        raise RuntimeError("Degenerate region centroid (norm=0)")
    x, y, z = m / norm

    ra = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    dec = math.degrees(math.asin(z))
    return ra, dec


def _pointings_from_visit_regions(butler, instrument: str, include_calibs: bool, registry_where: str | None):
    """
    Yield (ra_deg, dec_deg) from visit.region centroid; optional WHERE filter and calibration inclusion.
    """
    instrument_clause = f"instrument='{instrument}'"
    if registry_where:
        where_clause = f"{instrument_clause} AND ({registry_where})"
    else:
        where_clause = instrument_clause

    recs = butler.registry.queryDimensionRecords("visit", where=where_clause)
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
            # Skip pathological regions; keep going
            continue


def _maybe_load_pointings_from_butler(
    repo: str,
    instrument: str = "Nickel",
    include_calibs: bool = False,
    registry_where: str | None = None,
) -> Iterable[Tuple[float, float]]:
    """Open repo and yield pointings from visit.region. Raises if none found."""
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


# ===========================
# FITS directory helpers
# ===========================

def _fits_paths(root: str | Path) -> Iterable[Path]:
    root = Path(root)
    exts = (".fits", ".fit", ".fz", ".fits.fz")
    for p in sorted(root.rglob("*")):
        low = str(p).lower()
        if p.suffix.lower() in exts or any(low.endswith(e) for e in exts):
            yield p


def _parse_ra_dec_from_header(hdr) -> Tuple[float, float]:
    """Return (ra_deg, dec_deg) using WCS center if possible, else header fallbacks."""
    from astropy.wcs import WCS
    from astropy.coordinates import SkyCoord
    import astropy.units as u

    # 1) Try WCS center
    try:
        w = WCS(hdr)
        nx = int(hdr.get("NAXIS1", 0))
        ny = int(hdr.get("NAXIS2", 0))
        if nx > 0 and ny > 0 and w.has_celestial:
            sky = w.pixel_to_world(nx / 2.0, ny / 2.0)
            return float(sky.ra.deg), float(sky.dec.deg)
    except Exception:
        pass

    # 2) CRVAL1/2 (degrees)
    if "CRVAL1" in hdr and "CRVAL2" in hdr:
        try:
            return float(hdr["CRVAL1"]), float(hdr["CRVAL2"])
        except Exception:
            pass

    # 3) Sexagesimal strings
    for rkey, dkey in [("OBJCTRA", "OBJCTDEC"), ("RA", "DEC")]:
        if rkey in hdr and dkey in hdr:
            val_r = hdr[rkey]
            val_d = hdr[dkey]
            try:
                sc = SkyCoord(val_r, val_d, unit=(u.hourangle, u.deg))
                return float(sc.ra.deg), float(sc.dec.deg)
            except Exception:
                try:
                    sc = SkyCoord(val_r, val_d, unit=(u.deg, u.deg))
                    return float(sc.ra.deg), float(sc.dec.deg)
                except Exception:
                    pass

    raise RuntimeError("No usable WCS/RA/DEC found in FITS header")


def pointings_from_fits_dir(fits_dir: str | Path) -> Iterable[Tuple[float, float]]:
    """Yield (ra_deg, dec_deg) for each FITS file with usable WCS/headers."""
    from astropy.io import fits
    for p in _fits_paths(fits_dir):
        try:
            with fits.open(p, memmap=True) as hdul:
                hdr = None
                # prefer first image HDU with data
                for hdu in hdul:
                    if getattr(hdu, "data", None) is not None:
                        hdr = hdu.header
                        break
                if hdr is None:
                    hdr = hdul[0].header
                ra, dec = _parse_ra_dec_from_header(hdr)
                yield ra, dec
        except Exception:
            continue


# ===========================
# Input selection
# ===========================

def load_pointings(args) -> Tuple[np.ndarray, np.ndarray]:
    """Resolve RA/Dec inputs based on CLI args; returns degrees arrays."""
    # Priority: FITS dir → CSV → Butler → ras/decs
    if args.fits_dir:
        ras, decs = zip(*pointings_from_fits_dir(args.fits_dir))
        return np.array(ras, float), np.array(decs, float)

    if args.csv and Path(args.csv).exists():
        df = pd.read_csv(args.csv)
        return df["ra"].to_numpy(float), df["dec"].to_numpy(float)

    if args.butler:
        ras, decs = zip(*_maybe_load_pointings_from_butler(
            args.butler,
            instrument=args.instrument,
            include_calibs=args.include_calibs,
            registry_where=args.registry_where,
        ))
        return np.array(ras, float), np.array(decs, float)

    if args.ras and args.decs:
        ras = np.array([float(x) for x in args.ras.split(",")], float)
        decs = np.array([float(x) for x in args.decs.split(",")], float)
        return ras, decs

    raise SystemExit("No pointings provided. Use --fits-dir, --csv, --butler, or --ras/--decs.")


# ===========================
# Main
# ===========================

def main():
    ap = argparse.ArgumentParser(description="Batch Gaia DR3 cones for LSST refcats.")
    # Inputs
    ap.add_argument("--fits-dir", default=None, help="Directory of FITS files to read pointings from")
    ap.add_argument("--csv", default=None, help="CSV with columns ra,dec (degrees)")
    ap.add_argument("--butler", default=None, help="Butler repo to read visit.region pointings")
    ap.add_argument("--instrument", default="Nickel", help="Instrument name in the Butler repo (default: Nickel)")
    ap.add_argument("--registry-where", default=None,
                    help="Extra WHERE for registry (SQL-like), e.g. \"physical_filter = 'I' AND day_obs>=20240601\"")
    ap.add_argument("--include-calibs", action="store_true",
                    help="Include calibration visits (flats/bias/darks) when reading from Butler")

    # Direct arrays (fallback)
    ap.add_argument("--ras", default=None, help="Comma-separated RAs in degrees")
    ap.add_argument("--decs", default=None, help="Comma-separated Decs in degrees")

    # Query/IO params
    ap.add_argument("--radius-deg", type=float, default=RADIUS_DEG_DEFAULT, help="Cone radius in degrees")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE_DEFAULT, help="Cones per TAP batch (upload table size)")
    ap.add_argument("--g-min", type=float, default=7.0, help="Min Gaia G mag (bright cut)")
    ap.add_argument("--g-max", type=float, default=20.5, help="Max Gaia G mag (faint cut)")
    ap.add_argument("--sleep", type=float, default=SLEEP_BETWEEN_DEFAULT, help="Pause between TAP jobs (seconds)")
    ap.add_argument("--max-retries", type=int, default=MAX_RETRIES_DEFAULT, help="Max retries per TAP batch")

    ap.add_argument("--outdir", default="./data/gaia_dr3_cones_batched", help="Directory for Parquet shards")
    ap.add_argument("--merged-parquet", default="./data/gaia_dr3_all_cones/gaia_dr3_all_cones.parquet",
                    help="Path for merged Parquet")
    ap.add_argument("--merged-csv", default="./data/gaia_dr3_all_cones/gaia_dr3_all_cones.csv",
                    help="Path for merged CSV (for convertReferenceCatalog)")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing shard files if present")

    args = ap.parse_args()

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    Path(args.merged_parquet).parent.mkdir(parents=True, exist_ok=True)
    Path(args.merged_csv).parent.mkdir(parents=True, exist_ok=True)

    # Load/prepare pointings
    ras, decs = load_pointings(args)
    ras, decs = uniq_pairs(ras, decs)
    print(f"Unique pointings: {len(ras)} | radius={args.radius_deg:.5f} deg | batch={args.batch_size}")

    # Process in batches
    shards: list[Path] = []
    pid0 = 0
    for k, (i, j, rr, dd, radii) in enumerate(make_batches(ras, decs, args.radius_deg, args.batch_size), start=1):
        up = pd.DataFrame({
            "ra": rr,
            "dec": dd,
            "rdeg": radii,
            "pid": np.arange(pid0, pid0 + len(rr), dtype=np.int64),
        })
        pid0 += len(rr)

        shard_path = outdir / f"gaia_dr3_batch_{k:04d}.parquet"
        if shard_path.exists() and not args.overwrite:
            shards.append(shard_path)
            print(f"[{k}] SKIP existing shard: {shard_path.name}")
            continue

        df = _run_one_batch(
            up,
            cols_sql=COLS_SQL,
            max_retries=args.max_retries,
            g_min=args.g_min,
            g_max=args.g_max,
        )
        df.columns = [c.lower() for c in df.columns]
        df.to_parquet(shard_path, index=False)
        shards.append(shard_path)
        print(f"[{k}] rows={len(df)} → {shard_path.name}")
        time.sleep(args.sleep)

    # Merge + dedup once
    if not shards:
        raise SystemExit("No shards created. Nothing to merge.")
    frames = [pd.read_parquet(p) for p in shards]
    merged = pd.concat(frames, ignore_index=True).drop_duplicates(subset="source_id")

    # Sanity check: columns expected by your gaia_dr3_config.py
    need = {
        "source_id", "ra", "dec", "ra_error", "dec_error",
        "parallax", "parallax_error",
        "pmra", "pmra_error", "pmdec", "pmdec_error",
        "ref_epoch",
        "phot_g_mean_mag", "phot_bp_mean_mag", "phot_rp_mean_mag",
    }
    missing = need - set(merged.columns)
    if missing:
        raise SystemExit(f"Missing expected columns: {sorted(missing)}")

    # Write outputs
    merged.to_parquet(args.merged_parquet, index=False)
    merged.to_csv(args.merged_csv, index=False)

    print(f"\nSaved merged Parquet: {args.merged_parquet}  (rows={len(merged)})")
    print(f"Saved merged CSV:     {args.merged_csv}      (rows={len(merged)})")
    print("Done.")


if __name__ == "__main__":
    main()
