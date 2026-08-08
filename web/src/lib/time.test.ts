import { describe, expect, it } from "vitest";
import {
  formatClock,
  formatDate,
  formatDateTime,
  formatDayHeading,
} from "./time";

/**
 * Dates are day-first everywhere in the app. The bug that prompted this:
 * milestone badges rendered through ``toLocaleDateString()`` came out in
 * the runtime locale, so "first hundred turns" read ``7/10/2026`` next to
 * ``6/24/2026`` -- one parseable as a day-first date, one not, and neither
 * in the format the app is meant to speak.
 *
 * The assertions build their input from local-time constructors, since
 * these formatters deliberately render in local time and the suite has to
 * pass in any timezone.
 */
const local = (
  y: number,
  m: number,
  d: number,
  hh = 0,
  mm = 0,
): string => new Date(y, m - 1, d, hh, mm).toISOString();

describe("formatDate", () => {
  it("renders day-first with dots", () => {
    expect(formatDate(local(2026, 7, 10))).toBe("10.07.2026");
  });

  it("zero-pads single-digit days and months", () => {
    // de-DE would give "8.8.2026"; the padding keeps mono columns aligned.
    expect(formatDate(local(2026, 8, 8))).toBe("08.08.2026");
  });

  it("is unambiguous for a date that is valid either way round", () => {
    // The pair from the milestone list that started this.
    expect(formatDate(local(2026, 6, 24))).toBe("24.06.2026");
    expect(formatDate(local(2026, 6, 1))).toBe("01.06.2026");
  });

  it("accepts a Date and epoch millis as well as a string", () => {
    const d = new Date(2026, 0, 31, 12, 0);
    expect(formatDate(d)).toBe("31.01.2026");
    expect(formatDate(d.getTime())).toBe("31.01.2026");
  });

  it("parses a backend stamp carrying microseconds", () => {
    // Derived milestone dates come straight from SQLite at 6 fractional
    // digits, not the 3 the ISO grammar specifies.
    expect(formatDate("2026-05-27T20:46:25.371206+00:00")).toMatch(
      /^\d{2}\.\d{2}\.2026$/,
    );
  });

  it("falls back rather than printing NaN", () => {
    expect(formatDate(null)).toBe("");
    expect(formatDate(undefined)).toBe("");
    expect(formatDate("")).toBe("");
    expect(formatDate("not a date", "unknown")).toBe("unknown");
  });
});

describe("formatClock", () => {
  it("renders 24-hour time", () => {
    expect(formatClock(local(2026, 8, 8, 14, 5))).toBe("14:05");
  });

  it("pads the hour and keeps midnight at 00", () => {
    expect(formatClock(local(2026, 8, 8, 9, 30))).toBe("09:30");
    expect(formatClock(local(2026, 8, 8, 0, 0))).toBe("00:00");
  });

  it("never emits AM/PM", () => {
    const out = formatClock(local(2026, 8, 8, 22, 45));
    expect(out).toBe("22:45");
    expect(out).not.toMatch(/[ap]\.?m/i);
  });

  it("falls back on unparseable input", () => {
    expect(formatClock("nope")).toBe("");
  });
});

describe("formatDateTime", () => {
  it("puts the day-first date before the 24-hour time", () => {
    expect(formatDateTime(local(2026, 12, 3, 7, 8))).toBe("03.12.2026 07:08");
  });

  it("falls back on unparseable input", () => {
    expect(formatDateTime("nope", "never")).toBe("never");
  });
});

describe("formatDayHeading", () => {
  it("keeps the weekday and uses the numeric format for the rest", () => {
    // 2026-08-08 is a Saturday.
    const out = formatDayHeading(local(2026, 8, 8, 10, 0));
    expect(out).toContain("08.08.2026");
    expect(out).toMatch(/^\w+, 08\.08\.2026$/);
  });

  it("stays unique per day so it can key a grouped list", () => {
    const a = formatDayHeading(local(2026, 8, 8, 1, 0));
    const b = formatDayHeading(local(2026, 8, 8, 23, 0));
    const c = formatDayHeading(local(2026, 8, 9, 1, 0));
    expect(a).toBe(b);
    expect(a).not.toBe(c);
  });

  it("falls back on unparseable input", () => {
    expect(formatDayHeading("nope", "unknown")).toBe("unknown");
  });
});
