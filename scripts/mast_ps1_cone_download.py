import mastcasjobs
from pathlib import Path
import os
import time
import numpy as np
import pandas as pd
from dotenv import load_dotenv

def main(ras, decs, merge_after=True, download_only=False, overwrite=False):
    # === Setup ===
    load_dotenv(Path(".env"))
    user = os.environ["CASJOBS_USERID"]
    pwd = os.environ["CASJOBS_PW"]
    cas = mastcasjobs.MastCasJobs(username=user, password=pwd, context="PanSTARRS_DR2")

    radius_deg = 0.3
    radius_arcmin = radius_deg * 60
    outdir = Path("data/ps1_cones")
    outdir.mkdir(parents=True, exist_ok=True)

    all_dfs = []

    for i, (ra, dec) in enumerate(zip(ras, decs)):
        outfile = outdir / f"ps1_cone_{ra:.5f}_{dec:.5f}.csv"
        table_name = f"ps1_cone_{i}_{int(time.time())}"

        # Clean up any existing table with the same name
        cas.drop_table_if_exists(table_name)

        if outfile.exists() and not overwrite:
            print(f"⏩ Skipping existing: {outfile}")
            if merge_after:
                df = pd.read_csv(outfile)
                all_dfs.append(df)
            continue

        if not download_only:
            # Clean any prior copy of the table (if reusing names)
            cas.drop_table_if_exists(table_name)

            # Submit the cone search query
            query = f"""
            SELECT m.*
            INTO MYDB.{table_name}
            FROM fGetNearbyObjEq({ra}, {dec}, {radius_arcmin}) AS nb
            JOIN MeanObjectView AS m ON nb.objID = m.objID
            """
            print(f"\n🛰️ Submitting query for RA={ra:.5f}, Dec={dec:.5f} → {table_name}")
            try:
                job_id = cas.submit(query, task_name=f"submit_{table_name}")
                print(f"📡 Submitted job {job_id}, monitoring...")

                # Wait for job to complete
                while True:
                    num, status = cas.status(job_id)
                    status = status.lower()
                    print(f"Job {job_id} status: {num} {status}")
                    if status == "finished":
                        print(f"✅ Job {job_id} completed.")
                        break
                    elif status in {"failed", "cancelled"}:
                        raise RuntimeError(f"❌ Job {job_id} failed or was cancelled.")
                    time.sleep(20)

            except Exception as e:
                print(f"❌ Submit or monitor failed: {e}")
                continue

        # Download results with fast_table
        try:
            table = cas.fast_table(table=table_name)
            df = table.to_pandas()
            df.to_csv(outfile, index=False)
            print(f"✅ Saved {len(df)} rows to {outfile}")
            if merge_after:
                all_dfs.append(df)
        except Exception as e:
            print(f"❌ Download failed: {e}")
            continue

        # Drop table after download
        try:
            cas.drop_table_if_exists(table_name)
            print(f"🧹 Dropped table '{table_name}'")
        except Exception as e:
            print(f"⚠️ Failed to drop table '{table_name}': {e}")

    # Optionally merge all CSVs
    if merge_after and all_dfs:
        merged = pd.concat(all_dfs, ignore_index=True)
        merged = merged.drop_duplicates(subset="objID")
        merged_outfile = outdir / "merged_ps1_cones.csv"
        merged.to_csv(merged_outfile, index=False)
        print(f"\n📋 Merged {len(all_dfs)} tables into {merged_outfile}")


# === Usage example ===
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
