# Language files

Everything the agent says or says differently per language is in `languages/si.yaml`, `ta.yaml` and `en.yaml`:
greetings, example lines, speaking rules, goodbye / yes / no phrases, speech-to-text codes, branch names, and how
numbers, times and dates are said (`numbers`, `time`, `dates`). Every file has exactly the same keys; the app
refuses to start, naming the file and key, if one is missing or misspelt.

For a native-speaker review (written by the assistant, not yet checked):

| Language | Keys to check |
|---|---|
| Sinhala | `greeting`, `style_rules`, `lines.*`, `numbers`, `time`, `dates` |
| Tamil | `greeting`, `mixing_rules`, `lines.*`. `time` and `dates` are `null`: Tamil times and dates are still said in English form; fill them in to switch them on |
| English | `greeting`, `lines.*` |

The dashboard greetings and spoken addresses override the file values; the files are only the defaults.
After editing a yaml file, restart the service (`sudo systemctl restart nailagent`); the files are read at startup.
Adding a language = one new file with a unique `menu_digit` (and its key in the dashboard language menu).
