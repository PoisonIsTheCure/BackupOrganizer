#!/usr/bin/env swift
// Generates AppIcon.iconset (all required PNG sizes) from a drawn design —
// a rounded-square gradient with an SF Symbol glyph — so the app has a real
// icon without depending on any external asset. Run via build_app.sh; not
// part of the Swift package build itself.

import AppKit

let sizes: [(Int, String)] = [
    (16, "icon_16x16"), (32, "icon_16x16@2x"),
    (32, "icon_32x32"), (64, "icon_32x32@2x"),
    (128, "icon_128x128"), (256, "icon_128x128@2x"),
    (256, "icon_256x256"), (512, "icon_256x256@2x"),
    (512, "icon_512x512"), (1024, "icon_512x512@2x"),
]

func drawIcon(size: Int) -> NSImage {
    let image = NSImage(size: NSSize(width: size, height: size))
    image.lockFocus()

    let rect = NSRect(x: 0, y: 0, width: size, height: size)
    let corner = CGFloat(size) * 0.225
    let path = NSBezierPath(roundedRect: rect, xRadius: corner, yRadius: corner)
    let gradient = NSGradient(colors: [
        NSColor(calibratedRed: 0.20, green: 0.45, blue: 0.95, alpha: 1.0),
        NSColor(calibratedRed: 0.10, green: 0.75, blue: 0.65, alpha: 1.0),
    ])
    gradient?.draw(in: path, angle: -60)

    let symbolSize = CGFloat(size) * 0.56
    let config = NSImage.SymbolConfiguration(pointSize: symbolSize, weight: .medium)
        .applying(.init(hierarchicalColor: .white))
    if let symbol = NSImage(systemSymbolName: "arrow.triangle.2.circlepath.icloud", accessibilityDescription: nil)?
        .withSymbolConfiguration(config) {
        let symbolRect = NSRect(
            x: (CGFloat(size) - symbol.size.width) / 2,
            y: (CGFloat(size) - symbol.size.height) / 2,
            width: symbol.size.width, height: symbol.size.height)
        symbol.draw(in: symbolRect, from: .zero, operation: .sourceOver, fraction: 0.96)
    }

    image.unlockFocus()
    return image
}

let outputDir = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.iconset"
try? FileManager.default.createDirectory(atPath: outputDir, withIntermediateDirectories: true)

for (size, name) in sizes {
    let image = drawIcon(size: size)
    guard let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else {
        FileHandle.standardError.write("Failed to render \(name)\n".data(using: .utf8)!)
        exit(1)
    }
    let path = "\(outputDir)/\(name).png"
    try? png.write(to: URL(fileURLWithPath: path))
    print("wrote \(path)")
}
