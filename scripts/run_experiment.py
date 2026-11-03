"""Execute the experiment top-to-bottom in a fresh kernel of this interpreter."""
import argparse
from pathlib import Path
import sys

import nbformat
from nbclient import NotebookClient
from jupyter_client import KernelManager

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--notebook", type=Path, default=ROOT / "notebooks/synthetic_attribution_experiment_updated.ipynb")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/experiments/stage1/executed.ipynb")
    args = parser.parse_args()
    source = args.notebook.resolve()
    notebook = nbformat.read(source, as_version=4)
    nbformat.validate(notebook)
    kernel = KernelManager(kernel_name="python3")
    # Do not accidentally use a system kernel lacking the project dependencies.
    kernel.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
    def progress(cell, cell_index):
        if cell.cell_type == "code":
            print(f"Executing cell {cell_index + 1}/{len(notebook.cells)}", flush=True)
    client = NotebookClient(notebook, km=kernel, timeout=1200, on_cell_start=progress,
                            resources={"metadata": {"path": str(ROOT)}})
    try:
        client.execute()
    finally:
        # An explicitly provided kernel manager belongs to this script, not
        # nbclient; stop its process and remove connection files ourselves.
        if kernel.has_kernel:
            kernel.shutdown_kernel(now=True)
        kernel.cleanup_resources()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(notebook, args.output)
    print("Executed notebook:", args.output.resolve())
    for cell in notebook.cells:
        for output in cell.get("outputs", []):
            if output.output_type == "stream" and "Results:" in output.text:
                print(output.text)


if __name__ == "__main__":
    main()
