# Kids' Name Prediction

A browser-based name suggestion chatbot. It offers name ideas and looks up
meanings from a local SQLite database. Suggestions are inspiration, not a
prediction about a child's future or personality. It uses Python's standard
library; no API key or additional package is required.

## Run

From this folder, start the app:

```powershell
.\chatbot\Scripts\python.exe app.py
```

If you have Python installed separately, `python app.py` works too. On first
start, the app creates `kids_names.db`, creates its `kids_names` table, and
loads the starter names from `kids_names_seed.json`. Open
http://127.0.0.1:8000 in your browser. Stop the server with **Ctrl+C**.

## Deploy to Vercel

The project includes a WSGI entrypoint in `app.py` and a `vercel.json`
configuration. Import this repository in Vercel and deploy it from the project
root. The SQLite database is generated from `kids_names_seed.json` in the
serverless function's temporary directory; serverless storage is not
persistent. Keep permanent edits to the starter list in `kids_names_seed.json`.

## Get name ideas

Try `suggest baby names`, `suggest girl names`, `suggest neutral nature names`,
or `suggest names starting with A`. Ask `what does Aurora mean` to look up a
name. Each suggestion includes its meaning, origin, and style.

To add or change starter names, edit `kids_names_seed.json` and restart the
app. Seeded entries are updated on startup while other database entries are
left intact. Filters are based on the small starter list, so some combinations
may not have matches.

The greetings and bot information replies are stored in `intents.json`.

## Run tests

```powershell
.\chatbot\Scripts\python.exe -m unittest discover -s tests -v
```
