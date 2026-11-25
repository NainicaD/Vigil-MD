"""Exports of search results to PDF and Excel (api/export.py serves them).

  filters.py   parse and validate the filters of a request (pure)
  data.py      fetch the rows: recall finder and search list from the local search index (no
               BigQuery), a device page from the summary tables (the same few MB as the page)
  document.py  what a file holds (title, filters, notes, tables), shared by both formats
  pdf.py       build a PDF (reportlab)
  xlsx.py      build an Excel file (openpyxl, write-only)
"""
