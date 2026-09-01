import fs from "node:fs";

const notebookPaths = [
  "02-invoke-mcp.ipynb",
  "03-logic app script.ipynb",
  "03-logic app script-onetask.ipynb",
];

const logicAppNotebooks = new Set([
  "03-logic app script.ipynb",
  "03-logic app script-onetask.ipynb",
]);

const environmentUrlSource = [
  "import os\n",
  "from dotenv import load_dotenv\n",
  "\n",
  "load_dotenv()\n",
  'url = os.environ["LOGIC_APP_CALLBACK_URL"]\n',
];

for (const notebookPath of notebookPaths) {
  if (!fs.existsSync(notebookPath)) {
    continue;
  }

  const notebook = JSON.parse(fs.readFileSync(notebookPath, "utf8"));

  for (const cell of notebook.cells ?? []) {
    if (cell.cell_type === "code") {
      cell.execution_count = null;
      cell.outputs = [];
    }

    if (!logicAppNotebooks.has(notebookPath) || !Array.isArray(cell.source)) {
      continue;
    }

    cell.source = cell.source.flatMap((line) => {
      const hasExposedCallback =
        line.includes("logic.azure.com") && line.includes("sig=");
      return hasExposedCallback ? environmentUrlSource : [line];
    });
  }

  fs.writeFileSync(notebookPath, `${JSON.stringify(notebook, null, 1)}\n`);
}
