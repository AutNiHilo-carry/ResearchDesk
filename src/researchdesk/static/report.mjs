// Render the report formats produced by ResearchDesk using text and DOM nodes.
const heading = /^(#{1,6})\s+(.+)$/;
const listItem = /^\s*(?:(\d{1,6})[.)]|[-*+])\s+(.+)$/;
const quote = /^\s*>\s?(.*)$/;
const fence = /^\s*((?:`{3,})|(?:~{3,}))/;
const rule = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const separator = /^:?-{3,}:?$/;
const inline = /(`([^`]+)`|\*\*(.+?)\*\*|\[(E[0-9a-f]{16})\])/g;

function cells(line) {
  const text = line?.trim();
  if (!text?.startsWith("|") || !text.endsWith("|")) return null;
  return text.slice(1, -1).split(/(?<!\\)\|/).map((cell) => cell.trim().replaceAll("\\|", "|"));
}

export function parseReport(markdown) {
  const lines = markdown.split(/\r?\n/);
  const blocks = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i++];
    if (!line.trim()) continue;
    const opened = line.match(fence);
    const header = line.match(heading);
    const item = line.match(listItem);
    const quoted = line.match(quote);
    const columns = cells(line);
    const divider = columns && cells(lines[i]);
    if (opened) {
      const code = [];
      const marker = opened[1];
      const closed = new RegExp(`^\\s*${marker[0]}{${marker.length},}\\s*$`);
      while (i < lines.length && !closed.test(lines[i])) code.push(lines[i++]);
      if (i < lines.length) i++;
      blocks.push({ type: "code", text: code.join("\n") });
    } else if (header) {
      blocks.push({ type: "heading", level: header[1].length, text: header[2] });
    } else if (rule.test(line)) {
      blocks.push({ type: "rule" });
    } else if (divider && divider.length === columns.length && divider.every((cell) => separator.test(cell))) {
      i++;
      const rows = [];
      let row;
      while ((row = cells(lines[i]))?.length === columns.length) {
        rows.push(row);
        i++;
      }
      blocks.push({ type: "table", columns, rows });
    } else if (item) {
      const ordered = item[1] !== undefined;
      const items = [{ number: Number(item[1]), text: item[2] }];
      let next;
      while ((next = lines[i]?.match(listItem)) && (next[1] !== undefined) === ordered) {
        items.push({ number: Number(next[1]), text: next[2] });
        i++;
      }
      blocks.push({ type: "list", ordered, items });
    } else if (quoted) {
      const content = [quoted[1]];
      let next;
      while ((next = lines[i]?.match(quote))) {
        content.push(next[1]);
        i++;
      }
      blocks.push({ type: "quote", text: content.join("\n") });
    } else {
      blocks.push({ type: "paragraph", text: line });
    }
  }
  return blocks;
}

function appendInline(node, text, makeCitation) {
  const doc = node.ownerDocument;
  let offset = 0;
  for (const token of text.matchAll(inline)) {
    node.append(text.slice(offset, token.index));
    const [, raw, code, strong, evidenceId] = token;
    if (evidenceId) {
      node.append(makeCitation(evidenceId) ?? raw);
    } else if (code !== undefined) {
      const codeNode = doc.createElement("code");
      codeNode.textContent = code;
      node.append(codeNode);
    } else {
      const emphasis = doc.createElement("strong");
      appendInline(emphasis, strong, makeCitation);
      node.append(emphasis);
    }
    offset = token.index + raw.length;
  }
  node.append(text.slice(offset));
}

export function renderMarkdown(container, markdown, makeCitation) {
  const doc = container.ownerDocument;
  const withText = (tag, text) => {
    const node = doc.createElement(tag);
    appendInline(node, text, makeCitation);
    return node;
  };
  for (const block of parseReport(markdown)) {
    let node;
    switch (block.type) {
      case "heading":
        node = withText(`h${Math.min(block.level + 2, 6)}`, block.text);
        break;
      case "code": {
        node = doc.createElement("pre");
        const code = doc.createElement("code");
        code.textContent = block.text;
        node.append(code);
        break;
      }
      case "rule":
        node = doc.createElement("hr");
        break;
      case "table": {
        node = doc.createElement("div");
        node.className = "table-scroll";
        const table = doc.createElement("table");
        const head = doc.createElement("thead");
        const headerRow = doc.createElement("tr");
        for (const column of block.columns) {
          const header = withText("th", column);
          header.scope = "col";
          headerRow.append(header);
        }
        head.append(headerRow);
        const body = doc.createElement("tbody");
        for (const row of block.rows) {
          const tableRow = doc.createElement("tr");
          tableRow.append(...row.map((cell) => withText("td", cell)));
          body.append(tableRow);
        }
        table.append(head, body);
        node.append(table);
        break;
      }
      case "list":
        node = doc.createElement(block.ordered ? "ol" : "ul");
        for (const item of block.items) {
          const li = withText("li", item.text);
          if (block.ordered) li.value = item.number;
          node.append(li);
        }
        break;
      case "quote":
        node = withText("blockquote", block.text);
        break;
      default:
        node = withText("p", block.text);
    }
    container.append(node);
  }
}
