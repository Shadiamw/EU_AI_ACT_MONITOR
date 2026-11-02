# Model 3 - Deadline and escalation timeline (Phase 4)

`model3-timeline-phase4.png` is the Phase 2 Model 3 figure, extracted unchanged from the Phase 2 report (it is a raster image; there is no diagram source).

It still matches the implementation on main 9b2e587, so it was not redrawn:

- The deadline is an absolute UTC timestamp counted from the first serious assignment, not from detection.
- A revision to a stricter tier keeps the same anchor, so the deadline gets shorter.
- A revision to a looser tier does not extend the deadline unless a rationale is recorded.
- Each of the 50, 80 and 100 % reminders fires once; 49, 79 and 99 % never fire.
- The console shows the "legal window may already be running" indicator from detected_at (app/static/app.js).

The tests for each rule are listed in `phase4_models.md`, section 2.
