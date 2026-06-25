#!/usr/bin/env node
/**
 * Dependency-free PWA icon generator.
 *
 * Rasterises the Aiko crescent-moon mark into the PNG sizes the web app
 * manifest + iOS home screen need, writing them to ``web/public/icons/``.
 * Uses only Node built-ins (``zlib`` for the IDAT stream), so there's no
 * image library to install. Re-run with ``npm run gen:icons`` whenever the
 * mark changes.
 *
 * The crescent is the same construction as ``public/aiko.svg`` (a filled
 * moon circle minus an offset "cut" circle) so the favicon and the app
 * icons stay visually identical.
 */
import zlib from "node:zlib";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(__dirname, "..", "public", "icons");

// ── PNG encoder (RGBA, 8-bit, no interlacing) ───────────────────────
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++)
    c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const typeBuf = Buffer.from(type, "latin1");
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([len, typeBuf, data, crc]);
}

function encodePng(width, height, rgba) {
  const sig = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // colour type: RGBA
  ihdr[10] = 0; // compression
  ihdr[11] = 0; // filter
  ihdr[12] = 0; // interlace
  const stride = width * 4;
  const raw = Buffer.alloc((stride + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (stride + 1)] = 0; // filter type 0 (none) per scanline
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, y * stride + stride);
  }
  const idat = zlib.deflateSync(raw, { level: 9 });
  return Buffer.concat([
    sig,
    chunk("IHDR", ihdr),
    chunk("IDAT", idat),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

// ── Crescent mark ───────────────────────────────────────────────────
const lerp = (a, b, t) => a + (b - a) * t;
const hex = (h) => [
  parseInt(h.slice(1, 3), 16),
  parseInt(h.slice(3, 5), 16),
  parseInt(h.slice(5, 7), 16),
];

const TOP = hex("#1a1238"); // gradient top (matches body bg family)
const BOT = hex("#2a1d52"); // gradient bottom
const MOON = hex("#c4b5fd"); // brand lavender

function drawIcon(size) {
  const rgba = Buffer.alloc(size * size * 4);
  const s = size / 512; // design space is 512×512
  const cx1 = 256 * s,
    cy1 = 256 * s,
    r1 = 150 * s; // moon
  const cx2 = 312 * s,
    cy2 = 220 * s,
    r2 = 128 * s; // cut (offset up-right)
  const SS = 4; // supersample for anti-aliased crescent edge
  for (let y = 0; y < size; y++) {
    const tBg = y / (size - 1);
    const bg = [
      lerp(TOP[0], BOT[0], tBg),
      lerp(TOP[1], BOT[1], tBg),
      lerp(TOP[2], BOT[2], tBg),
    ];
    for (let x = 0; x < size; x++) {
      let cov = 0;
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const px = x + (sx + 0.5) / SS;
          const py = y + (sy + 0.5) / SS;
          const inMoon = (px - cx1) ** 2 + (py - cy1) ** 2 <= r1 * r1;
          const inCut = (px - cx2) ** 2 + (py - cy2) ** 2 <= r2 * r2;
          if (inMoon && !inCut) cov++;
        }
      }
      cov /= SS * SS;
      const i = (y * size + x) * 4;
      rgba[i] = Math.round(lerp(bg[0], MOON[0], cov));
      rgba[i + 1] = Math.round(lerp(bg[1], MOON[1], cov));
      rgba[i + 2] = Math.round(lerp(bg[2], MOON[2], cov));
      rgba[i + 3] = 255;
    }
  }
  return rgba;
}

// ── Emit ────────────────────────────────────────────────────────────
const TARGETS = [
  ["icon-192.png", 192],
  ["icon-512.png", 512],
  ["apple-touch-icon.png", 180],
];

fs.mkdirSync(OUT_DIR, { recursive: true });
for (const [name, size] of TARGETS) {
  const png = encodePng(size, size, drawIcon(size));
  fs.writeFileSync(path.join(OUT_DIR, name), png);
  console.log(`[gen-icons] wrote ${name} (${size}×${size}, ${png.length} bytes)`);
}
console.log(`[gen-icons] done -> ${OUT_DIR}`);
