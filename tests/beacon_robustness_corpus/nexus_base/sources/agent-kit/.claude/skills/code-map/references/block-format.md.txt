# Beacon blocks: the format

A beacon block is an ordinary comment, written in the file's own comment syntax, that labels the code next to it. Compilers and interpreters ignore it; the code-map script reads it.

## Two kinds

**`kind=ast`: one symbol.** The block sits directly above a function, class, method or module-level constant (above its decorators, if it has any) and attaches to that definition. It needs no closing block: the definition's own extent is the extent. This is the kind to use almost always.

```python
# @beacon[
#   id=billing@3,
#   slice_labels=billing,api,
#   kind=ast,
#   comment=POST /invoices: check, price and store one invoice,
# ]
@router.post("/invoices")
def create_invoice(request):
    """Check the request, price each line and store the invoice."""
```

**`kind=span`: a region.** An opening block and a closing `@beacon-close` block with the same `id` mark any stretch of lines: a whole file, a section that crosses several definitions, a critical stretch inside a long function, or code in a language with no symbol support (SQL, shell, YAML).

```python
# @beacon[
#   id=billing@pricing-rules,
#   slice_labels=billing,
#   kind=span,
#   show_span=false,
#   comment=Every pricing rule the invoice path applies, in the order it applies them,
# ]
# ... the region ...
# @beacon-close[
#   id=billing@pricing-rules,
# ]
```

## Fields

| Field | Required | What it holds |
|---|---|---|
| `id` | yes | Unique **within the file**. A short memorable form (`billing@3`, `billing@pricing-rules`) reads well; `tag` picks one for you. A span's closing block must repeat it exactly. |
| `slice_labels` | yes | One or more labels, comma-separated: lowercase letters, digits and hyphens. These decide which slices the symbol appears in. |
| `kind` | yes | `ast` or `span`. (When it is missing, the block is read as a span.) |
| `comment` | strongly recommended | One sentence saying what the symbol is for, written for someone choosing where to look. Commas are fine. Keep it on one line where you can; a long comment may continue on the following comment lines, as long as no continuation line starts with a field name and `=`. |
| `show_span` | spans only | `true` (the default) puts the region's source text into the slice: right for a short, critical region. `false` shows only the block: right for long regions and whole files. |

Write each field on its own comment line, end each with a comma, and close the block with `]` on a line of its own. Older blocks may carry other fields (for example `role=`); leave them as they are, and don't add them to new blocks.

## Languages

The block is the same everywhere; only the comment marker changes. The script reads Python, JavaScript/TypeScript, SQL, shell and YAML files; it does not read Markdown or Vue files.

- **Python, YAML, shell:** `#` (as in the examples above). Shell and YAML use `kind=span`.
- **JavaScript and TypeScript:** `//` line comments, not `/* */`. Works above functions, classes, `const` declarations, `export default` and arrow functions assigned to a `const`. A block above a TypeScript `interface`, `type` or `enum` attaches to nothing (those are not mapped): `check` reports it.

  ```typescript
  // @beacon[
  //   id=checkout@5,
  //   slice_labels=checkout,cache,
  //   kind=ast,
  //   comment=Prefetch cart prices in parallel and abort when the user leaves the page,
  // ]
  export async function prefetchPrices(cartIds: string[], signal?: AbortSignal) {
  ```

- **SQL:** `--` line comments, `kind=span`.

  ```sql
  -- @beacon[
  --   id=schema@invoices,
  --   slice_labels=billing,db,
  --   kind=span,
  --   comment=The invoices table and its indexes,
  -- ]
  CREATE TABLE invoices (...);
  -- @beacon-close[
  --   id=schema@invoices,
  -- ]
  ```

## What a slice shows for each block

For each tagged symbol or region, a slice shows its file (as a path relative to the repository root), its kind (`function`, `method`, `class`, `constant`, `const`, `region` ...), its signature, its labels, the block's `comment`, and then, in the full form, the documentation found beside it:

- the docstring of a Python function or class;
- a JSDoc/TSDoc block;
- comment lines directly above or directly below the block (blank lines are skipped over; code ends the run);
- for a span with `show_span=true`, the region's source text.

Function bodies are never included. So **the docstring and the comments next to the block are documentation that reaches every reader of the slice**: write them for that reader.

## Common mistakes

1. A span's closing `id` differs from its opening `id` by a character: the span silently breaks. `check` reports it.
2. A span with no closing block marks no region: the slice shows its labels and comment but no source text. `check` reports it.
3. `kind=ast` with no definition directly below it attaches to nothing. Use `kind=span`, or move the block. `check` reports it.
4. Two blocks with the same `id` in one file.
5. Code between a block and its symbol.
6. An empty or missing `slice_labels`: the label falls back to the part of the `id` before `@`, or the symbol appears in no slice; always write the labels.
7. A vague `comment` (`comment=helper`). Say what it does and when someone would look for it.
8. No docstring on a tagged function: the cheapest documentation there is, left out.
