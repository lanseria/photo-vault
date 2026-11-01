//
//  SyncLedger.swift
//  PhotoVaultSync
//
//  同步账本：PHAsset.localIdentifier → 这条在本机/服务器上的同步状态。
//
//  为什么需要它：
//    * 增量：modificationDate 没变且已经同步过的资产，下一轮直接跳过，
//      2 万张的相册第二次同步应该是「几秒扫完」而不是再传一遍；
//    * 断点：进程被杀后重启，能从盘上恢复「哪些传完了」，只补没传完的；
//    * 对账证据：服务器返回的 asset id 记在这里，服务器端删图/清理测试素材时能对上号。
//
//  落盘：JSON + 临时文件原子替换（写一半进程死掉不会把账本写坏），
//  路径 Application Support/SyncLedger.json。
//

import Foundation

// MARK: - 单条记录

/// 账本里的状态。故意不复用 Core 的 SyncItemState：账本是持久化格式，
/// 跟 UI 展示用的枚举解耦，以后 UI 状态改了不需要迁移旧账本。
enum LedgerState: String, Codable, Sendable, Equatable {
    case pending
    case uploading
    case done
    case skipped
    case failed

    /// 已经「不用再传」的终态（同步成功或服务器上已有）
    var isFinished: Bool { self == .done || self == .skipped }

    var itemState: SyncItemState {
        switch self {
        case .pending: return .pending
        case .uploading: return .uploading
        case .done: return .done
        case .skipped: return .skipped
        case .failed: return .failed
        }
    }
}

struct LedgerEntry: Codable, Equatable, Sendable {
    /// 记账时的 PHAsset.modificationDate；跟当前值不一致说明照片被编辑过，要重传
    var modificationDate: Date?
    var state: LedgerState
    /// 服务器返回的资产 id（duplicate 时是已存在那条的 id）——清理/排查用
    var serverAssetID: String?
    /// 上传给服务器的文件名（原始文件名）
    var name: String
    var bytes: Int64
    var updatedAt: Date
    var error: String?
    /// 当时上传到的目标库（gallery / material）。
    /// 用户在设置里切库后，同一张照片在另一个库里并不存在，不能靠 modificationDate 跳过。
    /// 旧版账本没有这个字段（nil）——按「相册」处理。
    var library: String?

    var isUpToDate: Bool { state.isFinished && error == nil }
}

// MARK: - 账本

struct SyncLedger: Codable, Equatable, Sendable {
    /// 以后格式变动时用来迁移；读到不认识的版本就……还是尽量读，字段都是容错的
    static let currentVersion = 1

    var version: Int = SyncLedger.currentVersion
    var records: [String: LedgerEntry] = [:]

    init() {}

    // MARK: 查询

    func entry(forAsset assetID: String) -> LedgerEntry? { records[assetID] }

    /// 增量核心：上次同步完成、照片没再被编辑过、而且目标库没换过 → 这轮不用管它
    func isUpToDate(assetID: String, modificationDate: Date?, library: String = "gallery") -> Bool {
        guard let entry = records[assetID], entry.isUpToDate else { return false }
        guard entry.modificationDate == modificationDate else { return false }
        return Self.libraryMatches(entry.library, library)
    }

    /// 旧版记录没有 library 字段，视为当初传去相册（gallery）
    static func libraryMatches(_ recorded: String?, _ current: String) -> Bool {
        guard let recorded else { return current == "gallery" }
        return recorded == current
    }

    func count(of state: LedgerState) -> Int {
        records.values.reduce(0) { $0 + ($1.state == state ? 1 : 0) }
    }

    /// 本机仍然存在的资产里，目标库里已同步 / 失败 各多少、已上传多少字节
    func statistics(for assetIDs: Set<String>,
                    library: String = "gallery") -> (synced: Int, failed: Int, bytes: Int64) {
        var synced = 0
        var failed = 0
        var bytes: Int64 = 0
        for id in assetIDs {
            guard let entry = records[id],
                  Self.libraryMatches(entry.library, library) else { continue }
            switch entry.state {
            case .done, .skipped:
                synced += 1
                bytes += entry.bytes
            case .failed:
                failed += 1
            case .pending, .uploading:
                break
            }
        }
        return (synced, failed, bytes)
    }

    // MARK: 写入

    mutating func markUploading(assetID: String, name: String,
                                modificationDate: Date?, library: String) {
        records[assetID] = LedgerEntry(modificationDate: modificationDate,
                                       state: .uploading,
                                       serverAssetID: records[assetID]?.serverAssetID,
                                       name: name,
                                       bytes: records[assetID]?.bytes ?? 0,
                                       updatedAt: Date(),
                                       error: nil,
                                       library: library)
    }

    mutating func markSynced(assetID: String, name: String, modificationDate: Date?,
                             serverAssetID: String?, bytes: Int64, library: String) {
        records[assetID] = LedgerEntry(modificationDate: modificationDate,
                                       state: .done,
                                       serverAssetID: serverAssetID,
                                       name: name,
                                       bytes: bytes,
                                       updatedAt: Date(),
                                       error: nil,
                                       library: library)
    }

    /// 服务器索引里已经有它 —— 记为跳过，同时记住服务器 id 方便以后对账
    mutating func markSkipped(assetID: String, name: String, modificationDate: Date?,
                              serverAssetID: String?, library: String) {
        records[assetID] = LedgerEntry(modificationDate: modificationDate,
                                       state: .skipped,
                                       serverAssetID: serverAssetID,
                                       name: name,
                                       bytes: 0,
                                       updatedAt: Date(),
                                       error: nil,
                                       library: library)
    }

    mutating func markFailed(assetID: String, name: String, modificationDate: Date?,
                             error: String, library: String) {
        records[assetID] = LedgerEntry(modificationDate: modificationDate,
                                       state: .failed,
                                       serverAssetID: records[assetID]?.serverAssetID,
                                       name: name,
                                       bytes: records[assetID]?.bytes ?? 0,
                                       updatedAt: Date(),
                                       error: error,
                                       library: library)
    }

    /// 单条回退成 pending（上传被用户停止时用：不算失败，下一轮重新规划）
    mutating func markPending(assetID: String) {
        guard var entry = records[assetID] else { return }
        entry.state = .pending
        entry.error = nil
        entry.updatedAt = Date()
        records[assetID] = entry
    }

    /// 进程在「正在上传」状态被杀掉：下次启动回落成 pending，让它们重新参与规划
    @discardableResult
    mutating func markInterruptedUploadsPending() -> Bool {
        var changed = false
        for (id, var entry) in records where entry.state == .uploading {
            entry.state = .pending
            entry.updatedAt = Date()
            records[id] = entry
            changed = true
        }
        return changed
    }

    /// 清掉失败 / 跳过的记录：让它们下一轮重新参与扫描与上传
    @discardableResult
    mutating func removeFinishedRecords() -> Int {
        let doomed = records.filter { $0.value.state == .failed || $0.value.state == .skipped }
        for key in doomed.keys { records.removeValue(forKey: key) }
        return doomed.count
    }

    /// 失败记录的资产 id（retryFailed 用）
    var failedAssetIDs: Set<String> {
        Set(records.compactMap { $0.value.state == .failed ? $0.key : nil })
    }
}

// MARK: - 落盘

/// 账本文件读写：JSON 编码 + 临时文件原子替换。
/// 跟引擎一样钉在主线程：文件只有几 MB，读写都在两轮扫描的空隙里做，没必要并发。
@MainActor
final class LedgerFile {
    let url: URL
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    init(url: URL) {
        self.url = url
        encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        encoder.dateEncodingStrategy = .secondsSince1970
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .secondsSince1970
    }

    /// Application Support/SyncLedger.json。目录不存在会顺手建出来；
    /// 建不出来（沙盒异常）就退到临时目录，宁可这轮账本不持久也不能让同步崩。
    static func defaultURL() -> URL {
        let fm = FileManager.default
        let directory = (try? fm.url(for: .applicationSupportDirectory,
                                     in: .userDomainMask,
                                     appropriateFor: nil,
                                     create: true)) ?? fm.temporaryDirectory
        return directory.appendingPathComponent("SyncLedger.json")
    }

    /// 读账本。文件不存在 / 解析失败都返回空账本并备份坏文件：
    /// 账本丢了顶多是多传一遍（服务端按 sha256 去重），不能因此阻塞同步。
    func load() -> SyncLedger {
        guard FileManager.default.fileExists(atPath: url.path) else { return SyncLedger() }
        do {
            let data = try Data(contentsOf: url)
            guard !data.isEmpty else { return SyncLedger() }
            return try decoder.decode(SyncLedger.self, from: data)
        } catch {
            let backup = url.deletingPathExtension()
                .appendingPathExtension("corrupt-\(Int(Date().timeIntervalSince1970)).json")
            try? FileManager.default.moveItem(at: url, to: backup)
            AppLog.shared.add(.warn, "账本读取失败，已备份为 \(backup.lastPathComponent)：\(error.localizedDescription)")
            return SyncLedger()
        }
    }

    /// 原子写：先写同目录临时文件再 replace，写到一半崩溃不会留下半截 JSON
    func save(_ ledger: SyncLedger) throws {
        let fm = FileManager.default
        let directory = url.deletingLastPathComponent()
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        var snapshot = ledger
        snapshot.version = SyncLedger.currentVersion
        let data = try encoder.encode(snapshot)
        let tmpURL = directory.appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString).tmp")
        try data.write(to: tmpURL, options: .atomic)
        if fm.fileExists(atPath: url.path) {
            _ = try fm.replaceItemAt(url, withItemAt: tmpURL, backupItemName: nil,
                                     options: [.usingNewMetadataOnly])
        } else {
            try fm.moveItem(at: tmpURL, to: url)
        }
    }
}
