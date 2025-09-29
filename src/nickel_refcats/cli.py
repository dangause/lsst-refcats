# src/nickel_refcats/cli.py
from __future__ import annotations
import argparse
import csv
from pathlib import Path
import numpy as np
from .pointings import (
    uniq_pairs, pointings_from_butler, pointings_from_fits_dir, normalize_where,
)
from .htm import cones_to_htm7

def main_cones():
    ap = argparse.ArgumentParser(description="Make cones.csv + htm7_list.txt from Butler/FITS/CSV/arrays")
    ap.add_argument("--fits-dir", default=None)
    ap.add_argument("--fits-recursive", action="store_true")
    ap.add_argument("--csv", default=None, help="CSV with columns ra,dec (degrees)")
    ap.add_argument("--butler", default=None, help="Local Butler repo to read visit.region centroids")
    ap.add_argument("--instrument", default="Nickel")
    ap.add_argument("--registry-where", default=None)
    ap.add_argument("--include-calibs", action="store_true")
    ap.add_argument("--ras", default=None)
    ap.add_argument("--decs", default=None)
    ap.add_argument("--radius-arcmin", type=float, default=6.0)
    ap.add_argument("--outdir", default="./data/monster_plan")
    args = ap.parse_args()

    # gather pointings
    if args.fits_dir:
        pts = list(pointings_from_fits_dir(args.fits_dir, args.fits_recursive))
        if not pts:
            raise SystemExit("No pointings from FITS")
        ras, decs = zip(*pts)
    elif args.csv and Path(args.csv).exists():
        import pandas as pd
        df = pd.read_csv(args.csv)
        ras = df["ra"].to_numpy(float)
        decs = df["dec"].to_numpy(float)
    elif args.butler:
        pts = list(pointings_from_butler(args.butler, args.instrument,
                                         args.include_calibs, normalize_where(args.registry_where)))
        if not pts:
            raise SystemExit("No visits from Butler query")
        ras, decs = zip(*pts)
    elif args.ras and args.decs:
        ras = [float(x) for x in args.ras.split(",")]
        decs = [float(x) for x in args.decs.split(",")]
    else:
        raise SystemExit("Provide --fits-dir or --csv or --butler or --ras/--decs")

    ras = np.asarray(ras, float)
    decs = np.asarray(decs, float)
    ras, decs = uniq_pairs(ras, decs)
    cones = [(float(r), float(d), args.radius_arcmin/60.0) for r, d in zip(ras, decs)]

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir/"cones.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ra_deg","dec_deg","radius_deg"])
        w.writerows(cones)
    htm7 = cones_to_htm7(cones, depth=7)
    (outdir/"htm7_list.txt").write_text(",".join(map(str, htm7))+"\n")

    print(f"Unique pointings: {len(cones)} | radius={args.radius_arcmin:.2f} arcmin")
    print(f"Wrote {outdir/'cones.csv'} and {outdir/'htm7_list.txt'} (n_htm7={len(htm7)})")


def main():
    # optional umbrella with subcommands
    ap = argparse.ArgumentParser(prog="nickel-refcats")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cones").set_defaults(func=main_cones)
    ns, _ = ap.parse_known_args()
    ns.func()
