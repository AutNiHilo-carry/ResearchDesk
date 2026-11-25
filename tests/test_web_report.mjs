import assert from "node:assert/strict";
import { test } from "node:test";
import { parseReport, renderMarkdown } from "../src/researchdesk/static/report.mjs";

test("renders the structured blocks used in research reports", () => {
  const report = [
    "# Backup", "",
    "2. Preserve the inputs.",
    "3. Verify the copy.",
    "> The measurement is unavailable.",
    "| Option | Evidence |",
    "| :--- | ---: |",
    "| Copy | Notes \\| excerpt |",
    "---",
    "```text",
    "| literal | code |",
    "```",
  ].join("\n");
  const blocks = parseReport(report);
  assert.deepEqual(blocks.map((block) => block.type), ["heading", "list", "quote", "table", "rule", "code"]);
  assert.deepEqual(blocks[1].items.map((item) => item.number), [2, 3]);
  assert.deepEqual(blocks[3].rows, [["Copy", "Notes | excerpt"]]);
  assert.equal(blocks[5].text, "| literal | code |");
});

test("only known citations are interactive and report HTML remains text", () => {
  const doc = {
    createElement(tag) {
      return { tag, ownerDocument: doc, contents: [], append(...children) { this.contents.push(...children); } };
    },
  };
  const root = doc.createElement("article");
  const known = "E0000000000001234";
  const unknown = "E0000000000004321";
  const clicked = [];
  const report = [
    `**Text [${known}]** \`${known}\` [${unknown}] <img src=x>`,
    "```text",
    `[${known}]`,
    "```",
    "| Test |",
    "| --- |",
    `| [${known}] |`,
  ].join("\n");
  renderMarkdown(root, report, (id) => {
    if (id !== known) return null;
    clicked.push(id);
    return doc.createElement("button");
  });
  assert.deepEqual(clicked, [known, known]);
  const [paragraph, pre, table] = root.contents;
  assert.equal(paragraph.contents.find((part) => part.tag === "strong").contents[1].tag, "button");
  assert.equal(paragraph.contents.find((part) => part.tag === "code").textContent, known);
  assert.ok(paragraph.contents.includes(`[${unknown}]`));
  assert.ok(paragraph.contents.includes(" <img src=x>"));
  assert.equal(pre.contents[0].textContent, `[${known}]`);
  assert.equal(table.contents[0].tag, "table");
});
