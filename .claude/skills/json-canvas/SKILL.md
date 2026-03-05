---
name: json-canvas
description: Create and edit JSON Canvas files (.canvas) with nodes, edges, groups, and connections. Use when working with .canvas files, creating visual canvases, mind maps, flowcharts, or when the user mentions Canvas files in Obsidian.
---

# JSON Canvas Skill

Create and edit valid JSON Canvas files (`.canvas`). Spec: https://jsoncanvas.org/spec/1.0/

## File Structure

```json
{ "nodes": [], "edges": [] }
```

## Node Types

All nodes require: `id` (16-char hex), `type`, `x`, `y`, `width`, `height`. Optional: `color`.

**Text:** `{ "type": "text", "text": "# Markdown content" }`
**File:** `{ "type": "file", "file": "path/to/note.md" }` (optional `subpath`: `"#Heading"`)
**Link:** `{ "type": "link", "url": "https://..." }`
**Group:** `{ "type": "group", "label": "Group Name" }` (optional `background`, `backgroundStyle`: cover|ratio|repeat)

Newlines in text: use `\n` not `\\n`.

## Edges

Required: `id`, `fromNode`, `toNode`. Optional: `fromSide`/`toSide` (top|right|bottom|left), `fromEnd`/`toEnd` (none|arrow), `color`, `label`.

## Colors

Hex: `"#FF0000"` or preset: `"1"` red, `"2"` orange, `"3"` yellow, `"4"` green, `"5"` cyan, `"6"` purple.

## IDs

16-character lowercase hex: `"6f0ad84f44ce9c17"`

## Layout

- x increases right, y increases down. Position = top-left corner.
- Small text: 200-300 × 80-150. Medium: 300-450 × 150-300. Large: 400-600 × 300-500.
- Space nodes 50-100px apart. 20-50px padding inside groups.

## Example

```json
{
  "nodes": [
    { "id": "8a9b0c1d2e3f4a5b", "type": "text", "x": 0, "y": 0, "width": 300, "height": 150, "text": "# Main Idea" },
    { "id": "1a2b3c4d5e6f7a8b", "type": "text", "x": 400, "y": 0, "width": 250, "height": 100, "text": "## Detail" }
  ],
  "edges": [
    { "id": "3c4d5e6f7a8b9c0d", "fromNode": "8a9b0c1d2e3f4a5b", "fromSide": "right", "toNode": "1a2b3c4d5e6f7a8b", "toSide": "left" }
  ]
}
```