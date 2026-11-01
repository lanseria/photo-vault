//
//  DebugOverrides.swift
//  PhotoVaultSync
//
//  **只用于 Debug 构建**的启动参数解析（Release 里这些参数没人读，行为完全不受影响）。
//  存在理由：端到端验证要连真实服务器，手工点 UI 既慢又容易点错。
//
//  支持：
//    -autostartSync              启动后自动跑一轮（BackgroundSyncManager 读这个标记）
//    -syncFromDateEpoch <秒>     Unix 秒（不是 ISO 字符串！例如 2026-11-01T00:00+08 = 1793462400）
//    -syncBaseURL <url>          覆盖服务器地址（同时清掉缓存的 effectiveBase）
//    -expectPlannedCount <N>     安全网：规划完成后条数不等于 N 就中止，绝不进入上传阶段
//
//  解析失败**不能静默降级**：宁可让这一轮直接失败，也不能因为参数写错就全量上传。
//  参数出错时 SyncEngine 会拒绝开始同步并把错误写进 AppLog / state.errorText。
//

import Foundation

struct DebugOverrides: Equatable {
    var fromDate: Date?
    var baseURL: String?
    /// 期望「待上传 + 服务器已有」的条数；e2e 时用来挡住意料之外的素材
    var expectPlannedCount: Int?

    var isEmpty: Bool {
        fromDate == nil && baseURL == nil && expectPlannedCount == nil
    }

    enum ParseError: LocalizedError, Equatable {
        case missingValue(flag: String)
        case invalidFromDate(String)
        case invalidCount(String)
        case invalidURL(String)

        var errorDescription: String? {
            switch self {
            case .missingValue(let flag):
                return "\(flag) 缺少参数值"
            case .invalidFromDate(let raw):
                return "-syncFromDateEpoch 需要 Unix 秒（例如 1793462400），收到：\(raw)"
            case .invalidCount(let raw):
                return "-expectPlannedCount 需要非负整数，收到：\(raw)"
            case .invalidURL(let raw):
                return "-syncBaseURL 不是合法地址：\(raw)"
            }
        }
    }

    static func parse(arguments: [String] = ProcessInfo.processInfo.arguments) throws -> DebugOverrides {
        var overrides = DebugOverrides()
        var index = 0
        while index < arguments.count {
            let flag = arguments[index]
            switch flag {
            case "-syncFromDateEpoch":
                let raw = try value(after: index, in: arguments, flag: flag)
                guard let seconds = TimeInterval(raw), seconds > 0 else {
                    throw ParseError.invalidFromDate(raw)
                }
                overrides.fromDate = Date(timeIntervalSince1970: seconds)
                index += 2
            case "-syncBaseURL":
                let raw = try value(after: index, in: arguments, flag: flag)
                guard ServerEndpoint.normalize(raw) != nil else {
                    throw ParseError.invalidURL(raw)
                }
                overrides.baseURL = raw
                index += 2
            case "-expectPlannedCount":
                let raw = try value(after: index, in: arguments, flag: flag)
                guard let count = Int(raw), count >= 0 else {
                    throw ParseError.invalidCount(raw)
                }
                overrides.expectPlannedCount = count
                index += 2
            default:
                index += 1
            }
        }
        return overrides
    }

    private static func value(after index: Int, in arguments: [String],
                              flag: String) throws -> String {
        guard index + 1 < arguments.count else { throw ParseError.missingValue(flag: flag) }
        return arguments[index + 1]
    }

    /// 日志里给人看的一行摘要
    var summary: String {
        var parts: [String] = []
        if let fromDate {
            parts.append("fromDate=\(ServerDate.display.string(from: fromDate))")
        }
        if let baseURL { parts.append("baseURL=\(baseURL)") }
        if let expectPlannedCount { parts.append("expectPlannedCount=\(expectPlannedCount)") }
        return parts.isEmpty ? "无" : parts.joined(separator: " · ")
    }
}
