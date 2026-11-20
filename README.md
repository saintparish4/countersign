# Countersign demo site

This branch is the GitHub Pages site at https://saintparish4.github.io/countersign/.
It is a guided operator console (a side menu of steps, the held-refund approval queue,
the agent's side, and the audit trail) that runs the
gateway in the browser with [Pyodide](https://pyodide.org), against the fake PayPal.
Nothing here touches PayPal.

- `app/` is a verbatim copy of `countersign/{__init__,gateway,paypal,policy,store}.py`
  and `tests/{__init__,fakes}.py` from `main` at `482db35`. To refresh it, copy those
  files again and update the commit id in `index.html`.
- `console.py` is the glue between the page and the gateway. It decides nothing.
- `wheels/` holds the two pure-Python wheels the gateway needs that Pyodide does not
  ship: `openscript-sdk` 0.1.0 and `structlog` 26.1.0, unmodified from PyPI.
- `.nojekyll` is required: Jekyll would drop `__init__.py`.

To run it locally: `python -m http.server --directory .` and open the printed address.
