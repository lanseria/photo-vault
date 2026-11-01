//
//  AppLog.swift
//  PhotoVaultSync
//
//  轻量日志缓冲：设置页可以直接看到"刚才发生了什么"，出问题时不用连 Xcode。
//  同时打到控制台（Console.app 里过滤 PhotoVaultSync 也能看）。
//

import Foundation
import Observation

@MainActor
@Observable
final class AppLog {
    static let shared = AppLog()

    enum Level: String, Sendable {
        case info, success, warn, error

        var symbol: String {
            switch self {
            case .info: return "·"
            case .success: return "✓"
            case .warn: return "!"
            case .error: return "✕"
            }
        }
    }

    struct Entry: Identifiable, Equatable, Sendable {
        let id = UUID()
        let date: Date
        let level: Level
        let message: String
    }

    private(set) var entries: [Entry] = []
    private let limit = 400

    func add(_ level: Level, _ message: String) {
        let entry = Entry(date: Date(), level: level, message: message)
        entries.append(entry)
        if entries.count > limit {
            entries.removeFirst(entries.count - limit)
        }
        #if DEBUG
        print("[\(level.rawValue.uppercased())] \(message)")
        #endif
    }

    func clear() {
        entries.removeAll()
    }

    var text: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return entries.map { "\(formatter.string(from: $0.date)) \($0.level.symbol) \($0.message)" }
            .joined(separator: "\n")
    }
}
