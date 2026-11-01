# Flow instructions

These files are the instructions each flow gives its bots. They are read every time the flow runs, so an edit takes effect on the next run, with no restart.

Planning (`services/first_plan.py`):

- `plan.md`: the brief the Master gets. `{research}` and `{decisions}` are filled from the files below.
- `plan.research.<mode>.md`: step 3. `full` asks every teammate to research on the web; `light` asks for short answers from what they know; `reuse` reads research already saved in Docs; `off` skips it.
- `plan.decisions.md` and `plan.decisions.none.md`: step 4, with or without questions to the person.

Placeholders are written `{name}`. To write a literal brace, double it: `{{` and `}}`. An unknown placeholder is left as written.
