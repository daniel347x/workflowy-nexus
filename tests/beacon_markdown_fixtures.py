"""U08 recon inputs frozen as quoted rows; no runtime scratch dependency.

Area-4 names/hashes are the original a345dc7 fixtures; U08 reproduced their
remaining base behavior at 8b981ee. Fields in supplementary tests add comments
and node UUIDs; these originals remain unmodified.
"""
M_FIXTURES = {
    "m02": (["# Title", "", "<!--", "@beacon[", "  id=h-a,", "  slice_labels=lab,",
              "  kind=ast,", "]", "-->", "", "Some prose that belongs to Title.", "",
              "## Section A", "", "text a"],
             "b97a5ad92b2f3f21a0b219a171e867b836f9fec431a7b0e04c3f98e882108900"),
    "m04": (["# Title", "", "## Section A", "", "text a", "", "<!--", "@beacon[",
              "  id=h-x,", "  slice_labels=lab,", "  kind=ast,", "]", "-->", "", "closing remark"],
             "ce6d6091ca0d20892bdb456f45aaa2a561841b42e7a116ff958895b792e7aadd"),
    "m05": (["# Title", "", "<!--", "@beacon[", "  id=h-a,", "  slice_labels=lab,",
              "]", "-->", "", "prose", "", "## Section A", "", "text a"],
             "7180ad6c9e31c245bdcf0c71da391e9108abf0f2d37d5ec362e92feefc962f31"),
    "m10": (["# Title", "", "<!--", "@beacon[", "  id=h-f,", "  slice_labels=lab,",
              "]", "-->", "```bash", "# install", "npm i", "```", "", "## Section A", "", "text a"],
             "49f8d52b71c623ab9d39816d8485b10eead44b5d3dcb2995192bc01b0304f4ba"),
}

E_CASES = [
    ("E01", b"# Doc\n\nText.\n\n\n\n", b"# Doc\n\nText.\n"),
    ("E02", "# Doc\n\nOne\u2028two.\n".encode(), "# Doc\n\nOne\u2028two.\n".encode()),
    ("E03", "# Head\x85ing\n\nText.\n".encode(), "# Head\x85ing\n\nText.\n".encode()),
    ("E04", "# Doc\n\n## Sec\u2029tion\n\nText.\n".encode(), "# Doc\n\n## Sec\u2029tion\n\nText.\n".encode()),
    ("E05", b"\n\n  \n", b""),
    ("E06", b"# Doc\r\n\r\nText.\r\n\r\n\r\n", b"# Doc\r\n\r\nText.\r\n"),
    ("E07", b"# Doc\n\nText.  ", b"# Doc\n\nText.  \n"),
    ("E08", b"# Doc\r\n\r\nText.\r\n", b"# Doc\r\n\r\nText.\r\n"),
]

DUP_SPAN_ROWS = ["# Title", "", "<!--", "@beacon[", "  id=m@sp,", "  slice_labels=lab-a,",
    "  kind=span,", "  comment=First span.,", "]", "-->", "", "Para one.", "", "<!--",
    "@beacon-close[", "  id=m@sp,", "]", "-->", "", "## Section B", "", "<!--", "@beacon[",
    "  id=m@sp,", "  slice_labels=lab-b,", "  kind=span,", "  comment=Second span, same id.,",
    "]", "-->", "", "Para two.", "", "<!--", "@beacon-close[", "  id=m@sp,", "]", "-->",
    "", "Para three."]
