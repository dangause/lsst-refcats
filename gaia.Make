refcat_gaia() {
  python scripts/gaia_fetch.py --butler "$REPO" --instrument Nickel \
    --registry-where "observation_reason='science'" --radius-deg 0.09 \
    --outdir ./data/gaia_dr3_cones_batched \
    --merged-parquet ./data/gaia_dr3_all_cones/gaia_dr3_all_cones.parquet \
    --merged-csv ./data/gaia_dr3_all_cones/gaia_dr3_all_cones.csv \
    --overwrite
  mkdir -p data_new/gaia-refcat
  convertReferenceCatalog data_new/gaia-refcat scripts/gaia_dr3_config.py \
    ./data/gaia_dr3_all_cones/gaia_dr3_all_cones.csv
  butler ingest-files -t direct "$REPO" gaia_dr3_20250728 \
    refcats/gaia_dr3_20250911 data_new/gaia-refcat/filename_to_htm.ecsv
  butler collection-chain "$REPO" refcats \
    refcats/gaia_dr3_20250911 refcats/panstarrs1_dr2_20250730 \
    --mode redefine
}
