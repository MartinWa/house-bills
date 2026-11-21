# Example parsers

Reviewed, wholly synthetic example parsers for the supported invoice structures. The
application image ships none of them: copy the files you want into an operator-owned
directory, mount it read-only at `/parsers`, and set
`HOUSE_BILLS_PARSER_PLUGINS=/parsers` (see
[deployed installation](../../deployment.md#parser-directory)).

Each `layout_*` file is one installed parser:

- `API_VERSION`, `FAMILY`, `VERSION` and `REVISION` declare its identity and contract
  version.
- Structural `matches(pages)` selects it; `parse(pages, *, candidate=None)` fills a
  partial candidate and returns `api.reconcile(...)`.
- `read_header(pages)` is optional and keeps format-specific header fields when section
  parsing fails.
- `configure_sections(...)` is the optional, host-validated cost-split hook. Layout 01
  implements it for configured waste fractions and printed demand charges.
- The `*_v2` files reuse their v1 sibling with a relative import
  (`from . import layout_01`), which the loader resolves inside the same directory.

Install the directory before configuring a vendor's invoice formats: the format choices
come from the installed parsers. After adding or changing a file, restart the container
and re-parse affected documents.

These files are examples, not a supported catalogue: a vendor whose printed structure
differs needs its own parser, kept private if it encodes private labels. Tests in
`tests/` hold every example to the documented contract.
