"""Everything vendor-specific.

One module per external thing: Document Intelligence, the LLM endpoint, Blob,
Cosmos, SQL, SharePoint, spreadsheet readers, tracing.

Each satisfies a protocol from `domain.ports`, so swapping a vendor means
writing a new file here and changing one line in the composition root — not
touching the rules that decide the money.
"""
