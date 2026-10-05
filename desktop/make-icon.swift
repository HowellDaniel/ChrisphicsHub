// Renders the CRISPprint Ghana Dock icon: the shop's four-diamond mark on its own ink.
// The mark is drawn rather than pasted from the logo file, so it stays crisp at 16px and
// carries no white plate into the Dock. Inks are the logo's crimson and warm grey, lifted
// for a dark tile (#AB1F23 / #808085 read as mud against near-black).
// Usage: swift make-icon.swift <output-dir-for-iconset>

import Foundation
import CoreGraphics
import ImageIO

let outDir = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : FileManager.default.currentDirectoryPath

func context(_ size: Int) -> CGContext {
    CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
              space: CGColorSpaceCreateDeviceRGB(),
              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
}

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat) -> CGColor {
    CGColor(red: red, green: green, blue: blue, alpha: 1)
}

let crimson = color(0.788, 0.227, 0.247)   // #c93a3f
let steel = color(0.604, 0.604, 0.627)     // #9a9aa0

/// A square stood on its corner: half-diagonal h around (cx, cy).
func diamond(_ ctx: CGContext, _ cx: CGFloat, _ cy: CGFloat, _ h: CGFloat) {
    ctx.move(to: CGPoint(x: cx, y: cy + h))
    ctx.addLine(to: CGPoint(x: cx + h, y: cy))
    ctx.addLine(to: CGPoint(x: cx, y: cy - h))
    ctx.addLine(to: CGPoint(x: cx - h, y: cy))
    ctx.closePath()
}

func render(_ px: Int) -> CGImage {
    let ctx = context(px)
    let s = CGFloat(px)
    let rect = CGRect(x: 0, y: 0, width: s, height: s)
    let badge = rect.insetBy(dx: s * 0.085, dy: s * 0.085)
    let radius = badge.width * 0.235
    let path = CGPath(roundedRect: badge, cornerWidth: radius, cornerHeight: radius, transform: nil)

    ctx.saveGState()
    ctx.addPath(path)
    ctx.clip()
    let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                              colors: [color(0.106, 0.082, 0.090), color(0.169, 0.125, 0.137)] as CFArray,
                              locations: [0, 1])!
    ctx.drawLinearGradient(gradient, start: CGPoint(x: 0, y: s), end: CGPoint(x: 0, y: 0), options: [])

    // The lockup's mark measured off the artwork: four diamonds standing on their
    // corners, each centre 1.27 half-diagonals out, so they nearly touch but never overlap.
    let mid = s * 0.5
    let h = s * 0.154
    let off = h * 1.27
    ctx.setFillColor(steel)
    diamond(ctx, mid, mid + off, h)     // top
    diamond(ctx, mid, mid - off, h)     // bottom
    ctx.fillPath()
    ctx.setFillColor(crimson)
    diamond(ctx, mid - off, mid, h)     // left
    diamond(ctx, mid + off, mid, h)     // right
    ctx.fillPath()

    ctx.addPath(path)
    ctx.setStrokeColor(CGColor(red: 1, green: 1, blue: 1, alpha: 0.14))
    ctx.setLineWidth(max(1, s * 0.006))
    ctx.strokePath()
    return ctx.makeImage()!
}

func png(_ image: CGImage, _ url: URL) {
    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil) else { return }
    CGImageDestinationAddImage(dest, image, nil)
    CGImageDestinationFinalize(dest)
}

let files: [(Int, String)] = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"), (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"), (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"), (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]
let dir = URL(fileURLWithPath: outDir, isDirectory: true)
try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
for (px, name) in files { png(render(px), dir.appendingPathComponent(name)) }
print("iconset written to \(outDir)")
