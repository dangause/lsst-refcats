from __future__ import annotations
import os, subprocess
from pathlib import Path

def have_cli(cmd: str) -> bool:
    try:
        subprocess.run([cmd, "--help"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return True
    except Exception:
        return False

def export_monster_htm7(repo: str, collections: str, htm7_csv: str,
                        out_dir: str = "monster_export", tar_path: str = "monster_bundle.tgz") -> str:
    """
    Run on the RSP. Exports the_monster_20250219 for the given HTM7 ids and creates a tar bundle.
    Returns path to the tarball.
    """
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    files_dir = out.parent/"monster_files"; files_dir.mkdir(exist_ok=True)
    tar = Path(tar_path)

    if have_cli("butler"):
        subprocess.check_call([
            "butler", "export", repo, str(out),
            "--collections", collections,
            "--dataset-type", "the_monster_20250219",
            "--where", f"htm7 IN ({htm7_csv})",
            "--transfer", "copy",
        ])
        subprocess.check_call(["butler", "transfer-files", str(out), str(files_dir)])
    else:
        import lsst.daf.butler as dafButler
        b = dafButler.Butler(repo, collections=collections)
        ids = [int(x) for x in htm7_csv.split(",") if x]
        refs = list(b.registry.queryDatasets("the_monster_20250219",
                                             where="htm7 IN (" + ",".join(map(str, ids)) + ")").expanded())
        b.export(repo, str(out), refs, transfer="copy")

    cwd = os.getcwd()
    try:
        os.chdir(out.parent)
        subprocess.check_call(["tar", "-czf", str(tar), out.name, "monster_files"])
    finally:
        os.chdir(cwd)

    return str(tar)
