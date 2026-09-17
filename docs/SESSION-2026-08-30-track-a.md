# SESSION LOG — מימוש מסלול A

**תאריך:** 2026-08-30
**ענף:** `track-a-implementation` (מ-`main` @ `e23d04f`)
**HEAD בסיום:** `219c46b`
**היקף:** 39 קומיטים · 79 קבצים · ~7,700 שורות · **307 בדיקות עוברות, 2 מדולגות**

מסמך זה מתעד את מה שנעשה, מה התגלה, ומה הוכרע. למסמך ההעברה הקצר ראו
[`HANDOFF.md`](HANDOFF.md); לאפיון ראו
[`specs/2026-08-25-safe-migration-gate-design.md`](superpowers/specs/2026-08-25-safe-migration-gate-design.md).

---

## 1. מה נעשה

תוכנית מימוש בת 20 משימות נכתבה
([`plans/2026-08-30-safe-migration-gate-track-a.md`](superpowers/plans/2026-08-30-safe-migration-gate-track-a.md))
ובוצעה עד הסוף. כל משימה: TDD (בדיקה נכשלת → מימוש → ירוקה) → סקירה ייעודית →
לולאת תיקון עד נקי → קומיט.

בסיום: סקירה רחבה על כל הענף, גל תיקונים יחיד, ו-re-review ממוקד על גל התיקונים.

### מבנה שנבנה

```
agent/
  state.py       GateState — חוזה יחיד; node_trail ו-errors עם reducers
  resources.py   resource_path() דרך sys._MEIPASS + העתקה ל-%LOCALAPPDATA%
  db.py          מנוע dry-run. ה-ROLLBACK. שני event listeners קריטיים
  sqlparse.py    פיצול ל-statements + היוריסטיקת trial-parse לדיאלקט
  rules.py       SG001–SG015 על ה-AST. שני צירים: risk_level / blocked
  knowledge.py   BM25 + lookup לפי rule_id
  knowledge/     15 מסמכי markdown, אחד לכל כלל
  rewrite.py     חוזה השכתוב + ולידטור האינווריאנטה
  llm.py         לקוח OpenRouter, truststore, מיסוך סודות, פרסור JSON
  prompts.py     חמישה פרומפטים
  nodes.py       תשעה נודים — ארבעה דטרמיניסטיים, חמישה LLM
  tools.py       שכבת הכלים שמסמך הקורס דורש
  graph.py       StateGraph, שלושה קצוות מותנים, שני שערי אישור
  cli.py         ה-REPL — הקובץ היחיד בפרויקט שמדפיס או קורא קלט
evals/           שלושה evals + fixtures + ground truth + rubric
data/seed.py     מסד דמו דטרמיניסטי (audit_log עם 150K שורות בשביל SG013)
build.spec       PyInstaller one-file
```

---

## 2. הממצא המרכזי של הסשן

**הסקירה הרחבה בסוף מצאה שני פגמים קריטיים שכל 20 הסקירות הממוקדות פספסו.**
שניהם הוכחו מול מסד נתונים אמיתי, לא נטענו מקריאה.

### פגם 1 — `COMMIT` בסקריפט של המשתמש ניטרל את כל ערובת ה-rollback

`dry_run_impact` העביר statements כמו שהם, `sqlglot` תרגם `COMMIT` ללא שינוי,
`exec_driver_sql` ביצע אותו — ואז ל-`transaction.rollback()` החיצוני לא נשאר מה לבטל.

```
script: "DELETE FROM users;\nCOMMIT;"
דווח:   statement 0 → 4 שורות, confidence=measured_demo_transpiled
בפועל:  0 מתוך 4 שורות נשארו במסד ה"לא נגוע"
```

`END`, `COMMIT TRANSACTION` ו-`commit` בלואר-קייס עשו אותו דבר. זה חל גם על
`db_url` שהמשתמש מספק — כלומר על מסדי ייצור. בנוסף המספר תויג `measured_demo_transpiled`,
תווית "נמדד", על ריצה שביצעה commit. ה-README הצהיר "אין נתיב קוד שמבצע COMMIT";
ההצהרה הייתה שקרית.

### פגם 2 — `check_invariant` אישר סקריפט "בטוח" שמוחק נתונים

רשת הביטחון שאמורה לתפוס מודל גרוע סבלה מאותו עיוורון, בשתי דרכים:

- `END` הוא מילה נרדפת ל-`COMMIT` ב-SQLite ולא נבדק:
  `BEGIN; DELETE FROM users; END; ROLLBACK;` עבר את שלוש הבדיקות ומחק הכול.
- בדיקת ה-statement האחרון בדקה רק את הזנב:
  `... ROLLBACK; DELETE FROM users; ROLLBACK;` עבר, וה-`DELETE` השני רץ מחוץ
  לכל טרנזקציה ונשמר.

### למה הסקירות הממוקדות פספסו

כל אחת מהן בדקה שהקוד עושה **מה שהתוכנית אמרה** — והתוכנית מעולם לא שקלה
שסקריפט הקלט מכיל בקרת טרנזקציות משלו. הסקירה הרחבה הייתה הראשונה שתקפה את
הערובה **מבחוץ** במקום לאמת אותה מבפנים.

זה הטיעון המעשי לשמור על סקירה סופית גם כשכל משימה כבר עברה.

### התיקון, ותגלית שעיצבה אותו

בקרת טרנזקציות לעולם אינה מבוצעת ב-dry run; היא מסוננת בשתי שכבות
(`dry_run_impact` וגם, הגנתית, `dry_run_script`), ונשארת ב-`impact` עם
`rows_affected=None` כדי שהאינדקסים יישארו מיושרים. `check_invariant` עבר
למעקב עומק טרנזקציה על סמך ה-AST: כל statement שמשנה נתונים חייב לשבת בתוך
טרנזקציה פתוחה שנסגרת ב-`ROLLBACK`.

**התגלית:** AST לבדו אינו מספיק.

| דיאלקט | `END` נפרס כ- |
|---|---|
| postgres | `exp.Commit` |
| mysql / sqlite / oracle | `exp.Column` |
| tsql | `exp.Command` |

בדיקת `isinstance(exp.Commit)` טהורה הייתה מתקנת את Postgres ומשאירה את
**SQLite — נתיב הדמו, שבו `END` מסוכן במיוחד — פגיע.** לכן הבודק בודק קודם סוג
צומת ואז נופל למילות מפתח מנורמלות. 33 איותים של COMMIT נוסו אחרי התיקון; כולם נתפסו.

---

## 3. ההכרעות

16 הכרעות התקבלו במהלך הביצוע. המלאות, עם הנימוקים, ב-ledger:
`.superpowers/sdd/2026-08-30-safe-migration-gate-track-a/progress.md`.

| # | הכרעה | עלות אם שגויה |
|---|---|---|
| 1 | `FILL_PLACEHOLDER` הוא block comment `/* FILL: condition */ 1 = 0` — צורת ה-`<--` שבאפיון אינה נפרסת ו-`validity` היה נכשל בכל שכתוב | מחרוזת סמן שונה מהאפיון. קוסמטי |
| 2 | callbacks ברמה העליונה של config (נמדד: מקונן = אפס אירועים) | Eval 2 מדווח אפסים. נתפס בהרצה ראשונה |
| 3 | probes של Eval 3 עוברים במסלולים האמיתיים; משפט תחום מפורש ב-`_cmd_ask` | `refusal_rate` מודד את הדבר הלא נכון |
| 4 | שומר בדיקות הרשת בודק `"network" in -m`, לא כל ביטוי `-m` | בדיקת רשת רצה בסוויטה מסוננת. רועש |
| 5 | `_raw_segments` עוקב אחרי מצב הערות — `;` בהערה פיצל statement והפיל `ParseFailure` שעוצר את הגרף | חיתוך שגוי בציטוט אקזוטי. מוכל |
| 6 | SG012 מוחזר להגדרת האפיון; ה-fixture של הבקרה השלילית היה השגוי | expand-migrations מקבלות MEDIUM. רעש, לא חסימה |
| 7 | שני "אירועים אמיתיים" בקורפוס נוסחו כתרחישים גנריים | אנקדוטה פחות חיה, יותר כנה |
| 8 | `scrub` מאציל ל-`mask_url` — היה עותק שני חלש שדלף סיסמאות עם `@` | import נוסף בתוך `agent/` |
| 9 | מדידה מול ה-DB הדמו לעולם אינה `measured_target` | ניסוח זהיר מדי, בכיוון הבטוח |
| 10 | תיקון ה-haystack של no-omissions — נשבר על כל statement רב-שורתי, גם במסלול הראשי | התאמה שגויה בפרוזה בהערה. מוכל |
| 11 | **נשאר פתוח בהחלטה:** `approve_write` מחשב payload לפני `interrupt()` — הניסוח בתוכנית שגוי, לא הקוד | אם `output_paths` תהפוך ללא-טהורה. ארבע שורות |
| 12 | `ask_line` ניתן להזרקה; משפט התחום בשני הענפים | תפר נוסף שאיש לא משתמש בו |
| 13 | fixture שנדחה אינו נמדד ב-rule recall | fixture אחד נמדד בציר אחר. חייב להיאמר ב-README |
| 14 | עמודות הטוקנים נדחו ל-Task 17 במקום לשכפל את ה-collector | CSV עם עמודות ריקות אם Task 17 היה נדחה |
| 15 | **תהליך:** בדיקה עצמית במקום re-review על מחיקת 3 imports מתים | הסקירה הסופית ראתה את הקומיט ממילא |
| 16 | Eval 3 צריך יותר מבקרה חיובית אחת + `must_contain` | עוד probes ושדה JSON אחד |

**שני ממצאי סוקר שנדחו לאחר בדיקה:**
`errors: []` ב-`evaluate` אינו מוחק שגיאות — יש `operator.add` reducer.
`python-dotenv` כן נבדק באריזה — הוא ב-`cli.py:281`, לפני הבאנר שה-smoke test הדפיס;
הנימוק של הסוקר היה הפוך.

---

## 4. מלכודות סביבה שנמדדו

| מה | ממצא |
|---|---|
| **פרוקסי TLS** | הרשת מריצה `palo-decrypt.scp.co.il`. `certifi` לא מכיר אותו, ולכן **כל** קריאת HTTPS מפייתון נכשלת ב-`CERTIFICATE_VERIFY_FAILED`. הפתרון: `truststore`, שמפנה למאגר התעודות של Windows |
| **`openrouter.ai` חסום** | ניתוק TLS יזום, לא בעיית תעודה. LangSmith ו-PyPI עוברים. **שלוש הרצות ה-eval לא בוצעו** |
| **pysqlite ו-DDL** | הדרייבר מבצע commit משתמע לפני כל DDL. `DROP TABLE` ב-dry run באמת מחק את הטבלה. תוקן בשני event listeners; יש בדיקת רגרסיה |
| **נעילת קורסור** | `SELECT` שלא נסגר מחזיק read lock; `DROP` אחריו נכשל ב-`database table is locked`. תוקן ב-`result.close()` |
| **`sqlglot`** | אין API לזיהוי דיאלקט. מומשה היוריסטיקת trial-parse ומתועדת ככזו |
| **`FakeListChatModel`** | לא מממש `with_structured_output` — ולכן נודי ה-LLM מפרסרים JSON בעצמם. זה גם מה שמאפשר להם לרוץ על מודלים חינמיים ב-OpenRouter שלא תומכים ב-tool-calling, כלומר בדיוק אלה ש-Eval 1 אמור למדוד |

---

## 5. מה נשאר

**חסום תשתית, לא החלטה.** `test_output.txt` (לוג pytest ישן שנכנס בטעות ב-`219c46b`)
עדיין tracked. `.gitignore` כבר עודכן; השינוי יושב unstaged:

```bash
git rm --cached test_output.txt && rm test_output.txt
git add .gitignore && git commit -m "chore: drop committed pytest log artifact"
```

**לפני הגשה:**
- `manifest.json` מכיל `<REPLACE_WITH_ORG_OR_USER>` — צריך remote אמיתי.
- שלוש הרצות ה-eval, כשה-OpenRouter ייפתח. הקוד והבדיקות האופליין שלמים.
- לאמת את חילוץ ספירת הטוקנים ב-Eval 2 בהרצה האמיתית הראשונה — `FakeListChatModel`
  לא מאכלס usage metadata, ולכן אופליין נבדק רק **שם** הדלי, לא הספירה.

**מגבלות מתועדות ב-README:** שלוש היוריסטיקות (SG007/SG008/SG015), שמדידה על
הדמו אינה מספר מהמסד של המשתמש, שהשופט ב-Eval 1 הוא גם אחד המועמדים, ושה-Eval
השלישי מודד ניתוב תחום דטרמיניסטי ב-CLI ולא היצמדות-תחום של המודל — `_cmd_ask`
אינו קורא למודל כלל.

**מסלול B (Desktop)** לא נגע. תוכנית נפרדת, לפי סעיף 12 באפיון.

---

## 6. מה שווה לקחת לסשן הבא

1. **סקירה סופית רחבה שווה את עלותה גם כשהכול ירוק.** שני הפגמים הקריטיים שרדו
   20 סקירות ממוקדות כי כולן אימתו מול התוכנית. רק תקיפה מבחוץ מצאה אותם.
2. **כשתוצאה נראית טובה מדי, היא כנראה טאוטולוגית.** Eval 3 דיווח 100% והתברר
   ש-12 מתוך 13 probes היו עוברים גם עם מסווג שמחזיר `True` תמיד.
3. **"זה לא ייתכן" הוא ניחוש. תמדוד.** התגלית על `END` בדיאלקטים שונים, והעובדה
   ש-callbacks מקוננים מקבלים אפס אירועים — שתיהן התגלו רק כי מישהו הריץ probe
   במקום להסתמך על התיעוד.
4. **תיעוד צריך fact-check, לא הגהה.** ה-README הצהיר ש-`demo.db` נמצא בריפו
   כשהוא gitignored — כל מי שהיה עוקב אחרי ההוראות היה נתקע.
