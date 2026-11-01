// make_live_photo.swift —— 造一对**真** Live Photo（静帧 + 动态），给模拟器相册喂素材用。
//
// 为什么不能只把 JPG 和 MOV 一起 addmedia：Photos 认 Live Photo 靠一对匹配的
// content identifier，静帧那边写在 Apple MakerNote 的 "17"（ImageIO 里是
// kCGImagePropertyMakerAppleDictionary），动态那边写在 QuickTime metadata
// `com.apple.quicktime.content.identifier`，另外动态还要有一条 timed metadata
// `com.apple.quicktime.still-image-time`（表示哪一帧是静帧）。缺一样就变成两张独立资产。
//
// 用法：
//   swiftc -O make_live_photo.swift -o make_live_photo
//   ./make_live_photo <静帧源.jpg|png> <动态源.mov> <输出目录> <stem> [content-id]
//
// 产物：<输出目录>/<stem>.HEIC + <输出目录>/<stem>.MOV，打印 content id。
import AVFoundation
import CoreGraphics
import Foundation
import ImageIO

func die(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

// MARK: 静帧：写 MakerApple(17) = content id
func writeStill(src: URL, dst: URL, contentID: String) {
    guard let srcRef = CGImageSourceCreateWithURL(src as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(srcRef, 0, nil)
    else { die("读不到源静帧：\(src.path)") }

    try? FileManager.default.removeItem(at: dst)
    let uti = "public.heic"
    guard let dest = CGImageDestinationCreateWithURL(dst as CFURL, uti as CFString, 1, nil)
    else { die("创建 HEIC 输出失败（宿主不支持 HEIC 编码？）") }

    var props: [CFString: Any] = [
        kCGImageDestinationLossyCompressionQuality: 0.85,
        kCGImagePropertyMakerAppleDictionary: ["17": contentID],
    ]
    // 顺手把源图的相机 EXIF 带过去（App 端按机型分组时能看出差别）
    if let srcProps = CGImageSourceCopyPropertiesAtIndex(srcRef, 0, nil) as? [CFString: Any],
       let exif = srcProps[kCGImagePropertyExifDictionary] as? [CFString: Any] {
        props[kCGImagePropertyExifDictionary] = exif
    }
    CGImageDestinationAddImage(dest, image, props as CFDictionary)
    guard CGImageDestinationFinalize(dest) else { die("写静帧失败：\(dst.path)") }
}

// MARK: 动态：透传视频轨 + content id + still-image-time timed metadata
func writeMotion(src: URL, dst: URL, contentID: String) {
    let asset = AVURLAsset(url: src)
    guard let videoTrack = asset.tracks(withMediaType: .video).first else {
        die("源视频没有视频轨：\(src.path)")
    }
    var fmtHint: CMFormatDescription?
    if let raw = videoTrack.formatDescriptions.first {
        fmtHint = (raw as! CMFormatDescription)
    }

    try? FileManager.default.removeItem(at: dst)
    let writer: AVAssetWriter
    let reader: AVAssetReader
    do {
        writer = try AVAssetWriter(outputURL: dst, fileType: .mov)
        reader = try AVAssetReader(asset: asset)
    } catch { die("初始化 reader/writer 失败：\(error)") }

    let trackOut = AVAssetReaderTrackOutput(track: videoTrack, outputSettings: nil)
    trackOut.alwaysCopiesSampleData = false
    guard reader.canAdd(trackOut) else { die("不能加视频输出") }
    reader.add(trackOut)

    let videoIn = AVAssetWriterInput(mediaType: .video, outputSettings: nil, sourceFormatHint: fmtHint)
    videoIn.expectsMediaDataInRealTime = false
    guard writer.canAdd(videoIn) else { die("不能加视频输入") }
    writer.add(videoIn)

    // metadata 输入必须带 format description（AVAssetWriterInputMetadataAdaptor 硬要求），
    // 否则直接抛 NSInvalidArgumentException。
    let metaSpec: [String: Any] = [
        kCMMetadataFormatDescriptionMetadataSpecificationKey_Identifier as String:
            "mdta/com.apple.quicktime.still-image-time",
        kCMMetadataFormatDescriptionMetadataSpecificationKey_DataType as String:
            "com.apple.metadata.datatype.int8",
    ]
    var metaDesc: CMMetadataFormatDescription?
    CMMetadataFormatDescriptionCreateWithMetadataSpecifications(
        allocator: kCFAllocatorDefault,
        metadataType: kCMMetadataFormatType_Boxed,
        metadataSpecifications: [metaSpec] as CFArray,
        formatDescriptionOut: &metaDesc
    )
    let metaIn = AVAssetWriterInput(mediaType: .metadata, outputSettings: nil, sourceFormatHint: metaDesc)
    metaIn.expectsMediaDataInRealTime = false
    guard writer.canAdd(metaIn) else { die("不能加 metadata 输入") }
    writer.add(metaIn)
    let adaptor = AVAssetWriterInputMetadataAdaptor(assetWriterInput: metaIn)

    // 电影级 metadata：content identifier（这是和静帧配对的 key）
    let idItem = AVMutableMetadataItem()
    idItem.identifier = .quickTimeMetadataContentIdentifier
    idItem.value = contentID as NSString
    writer.metadata = [idItem]

    guard writer.startWriting() else { die("startWriting 失败：\(String(describing: writer.error))") }
    writer.startSession(atSourceTime: .zero)
    guard reader.startReading() else { die("startReading 失败：\(String(describing: reader.error))") }

    // 视频样本透传
    let vSem = DispatchSemaphore(value: 0)
    videoIn.requestMediaDataWhenReady(on: DispatchQueue(label: "qa.video")) {
        while videoIn.isReadyForMoreMediaData {
            if let sb = trackOut.copyNextSampleBuffer() {
                if !videoIn.append(sb) {
                    FileHandle.standardError.write("append 样本失败：\(String(describing: writer.error))\n".data(using: .utf8)!)
                    videoIn.markAsFinished()
                    vSem.signal()
                    return
                }
            } else {
                videoIn.markAsFinished()
                vSem.signal()
                return
            }
        }
    }
    vSem.wait()

    // still-image-time：值 0 表示"这一帧就是静帧"（Apple 约定 int8）
    let stillTimeItem = AVMutableMetadataItem()
    // SDK 里没有 quickTimeMetadataStillImageTime 常量，直接按 mdta 命名空间写
    stillTimeItem.identifier = AVMetadataIdentifier("mdta/com.apple.quicktime.still-image-time")
    stillTimeItem.dataType = "com.apple.metadata.datatype.int8"
    stillTimeItem.value = NSNumber(value: 0)
    let range = CMTimeRange(start: .zero, duration: CMTime(value: 1, timescale: 30))
    let group = AVMutableTimedMetadataGroup(items: [stillTimeItem], timeRange: range)

    let mSem = DispatchSemaphore(value: 0)
    metaIn.requestMediaDataWhenReady(on: DispatchQueue(label: "qa.meta")) {
        if adaptor.append(group) == false {
            FileHandle.standardError.write("append 静帧时刻 metadata 失败\n".data(using: .utf8)!)
        }
        metaIn.markAsFinished()
        mSem.signal()
    }
    mSem.wait()

    let finishSem = DispatchSemaphore(value: 0)
    writer.finishWriting { finishSem.signal() }
    finishSem.wait()
    if writer.status != .completed {
        die("finishWriting 未完成：status=\(writer.status.rawValue) err=\(String(describing: writer.error))")
    }
}

// MARK: main
let args = CommandLine.arguments
guard args.count >= 5 else {
    die("用法：make_live_photo <still-src> <motion-src> <outdir> <stem> [content-id]")
}
let stillSrc = URL(fileURLWithPath: args[1])
let motionSrc = URL(fileURLWithPath: args[2])
let outDir = URL(fileURLWithPath: args[3], isDirectory: true)
let stem = args[4]
let contentID = args.count > 5 ? args[5] : UUID().uuidString.uppercased()

try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
let stillOut = outDir.appendingPathComponent("\(stem).HEIC")
let motionOut = outDir.appendingPathComponent("\(stem).MOV")

writeStill(src: stillSrc, dst: stillOut, contentID: contentID)
writeMotion(src: motionSrc, dst: motionOut, contentID: contentID)

let stillBytes = (try? FileManager.default.attributesOfItem(atPath: stillOut.path)[.size] as? Int) ?? 0
let motionBytes = (try? FileManager.default.attributesOfItem(atPath: motionOut.path)[.size] as? Int) ?? 0
print("content-id=\(contentID)")
print("still=\(stillOut.path) bytes=\(stillBytes)")
print("motion=\(motionOut.path) bytes=\(motionBytes)")
