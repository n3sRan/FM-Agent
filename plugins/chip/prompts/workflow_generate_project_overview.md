# Chip Verification Overview Generation

Generate the run-level `OVERVIEW.md` for a hardware analysis scope. It is a
reviewer-facing entry point to the available module specifications: help someone
understand the analyzed DUT or scope and where to find the detailed specs.

Read the generated spec-first input brief and every listed final standalone module
specification. Those specifications are the primary evidence for this Overview.
The input brief also names a raw HDL source inventory. You may inspect relevant
`.scala`/`.sc` and `.v`/`.sv`/`.svh` files listed there only when they usefully
supplement the summary. Do not run a build, elaboration, simulation, or test
command, and do not modify source files or module artifacts.

Write only `fm_agent/chip/overview/OVERVIEW.pending.md`. Do not write source
files, module artifacts, project-root files, or any other file.

Use the following five-section structure. Adapt the amount of detail to the
available evidence; it is not necessary to imitate any reference document's
parameter selection, interface layout, terminology, or length. A short, useful
summary is preferable to exhaustive reconciliation or repeated specification
content.

```markdown
# <analysis-derived DUT or analysis-scope name>

<short introduction>

## 1. Device Under Test (DUT) Description

### 1.1 Module Parameters

### 1.2 Interface List

### 1.3 Functional Description

## 2. Verification Requirements

### 2.1 Verification Objectives

### 2.2 Verification Plan Structure

## 3. Additional Notes

## 4. Bug Analysis

## 5. Specification Documents

<!-- FM_AGENT_SPEC_INDEX -->
```

Preserve code identifiers, paths, module names, and project names when citing
them. Do not invent unsupported hardware behavior or claim that a build,
elaboration, simulation, or test ran. Use `TBD` only when it helps communicate a
material unknown; the Overview may otherwise stay high-level. Treat generated
specifications as the primary evidence and raw HDL as supplementary evidence.

Use short paragraphs and compact tables when helpful. There is no fixed line
count or mandatory degree of interface expansion.

Write the sections at this level of detail:

- **1.1 Module Parameters:** summarize configuration, sizing, or other material
  parameters when useful. Use a compact table with exactly these columns:
  `| Parameter | Description |`. A brief statement is sufficient when values
  are not available.
- **1.2 Interface List:** describe the primary DUT's externally relevant
  interface in a compact table with exactly these columns:
  `| Port | Direction | Type | Description |`. When relevant `.v`/`.sv` source
  is available, use its module header and port declarations to supplement the
  interface list. Choose one clear primary interface view:
  - When a relevant RTL module header is available for the primary DUT, prefer an
    RTL-facing table. List the visible top-level ports and meaningful payload
    leaf signals as separate rows, including clock/reset when present. State each
    row's direction and declared or normalized hardware type/width. Regular,
    repeated indexed signal families may use a compact range when their common
    type and role are clear.
  - Otherwise, use the structured Chisel interface view, grouping bundle and
    `Vec` ports where that is the clearest available representation.
  Do not mix the two views by putting a long flattened RTL signal list inside a
  structured Chisel row's Description column. Use a brief note outside the
  table only when the alternate view is materially useful to the reader.
- **1.3 Functional Description:** use a short ordered list of the main observable
  responsibilities. Keep each item concise and do not reproduce the module
  specification, internal register inventory, or cycle-by-cycle implementation.
- **2.1 Verification Objectives:** state only a few useful black-box verification
  themes.
- **2.2 Verification Plan Structure:** use one short paragraph that directs readers
  to the detailed module specifications; do not reproduce their FG/FC/CK tree.
- **3. Additional Notes:** keep only directly useful scope or evidence notes, in a
  short paragraph or a few bullets.
- **4. Bug Analysis:** provide one brief future-debugging pointer; do not claim a
  bug was found or enumerate a debugging checklist.
- **5. Specification Documents:** keep this section brief. Leave the index marker
  on its own line; do not add a separate duplicate list of specification paths.

When the input brief identifies one supported root candidate, describe it as the
primary DUT. Other listed specifications may be mentioned as implementation
context and linked in section 5, but are not separate verification targets. When
it identifies multiple or uncertain root candidates, describe the selected
analysis scope without choosing an arbitrary chip-level top module; label
parameter and interface material by candidate where applicable. Do not describe
Pipeline phases, artifact eligibility, context-node classification, or other
Pipeline processing details unless directly needed to prevent a material
misunderstanding of the DUT.

The Overview is a guide to detailed specifications, not a replacement for them.

Leave the `<!-- FM_AGENT_SPEC_INDEX -->` marker unchanged and on its own line.
The generator replaces it with the verified specification-link index.
