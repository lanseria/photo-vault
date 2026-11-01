//
//  LibraryGroupingTests.swift
//  PhotoVaultSyncTests
//
//  图库按天分组 + 搜索词规范化的单测（纯函数，不依赖网络/PhotoKit）。
//  分组逻辑在 RemoteLibraryModel.groupByDay（UI/RemoteLibraryModel.swift）。
//

import XCTest
@testable import PhotoVaultSync

final class LibraryGroupingTests: XCTestCase {

    // 固定时区，避免测试机时区影响「今天/昨天」的判断
    private var calendar: Calendar = {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        cal.locale = Locale(identifier: "zh_Hans_CN")
        return cal
    }()

    /// 用「墙上时间」字符串造资产（和服务端 taken 的格式一致）
    private func asset(_ id: String, taken: String?, model: String = "iPhone 12 Pro Max") -> RemoteAsset {
        var item = RemoteAsset()
        item.id = id
        item.sha = String(repeating: "a", count: 64)
        item.taken = taken
        item.model = model
        return item
    }

    private func date(_ year: Int, _ month: Int, _ day: Int, _ hour: Int = 12, _ minute: Int = 0) -> Date {
        var comps = DateComponents()
        comps.year = year
        comps.month = month
        comps.day = day
        comps.hour = hour
        comps.minute = minute
        return calendar.date(from: comps)!
    }

    private func wallString(_ date: Date) -> String {
        let c = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
        return String(format: "%04d-%02d-%02dT%02d:%02d:%02d",
                      c.year!, c.month!, c.day!, c.hour!, c.minute!, c.second!)
    }

    // MARK: - 分组

    func testGroupsTodayAndYesterday() {
        let now = date(2026, 11, 1, 12, 0)
        let assets = [
            asset("a", taken: wallString(date(2026, 11, 1, 9, 30))),
            asset("b", taken: wallString(date(2026, 11, 1, 8, 0))),
            asset("c", taken: wallString(date(2026, 10, 31, 23, 59))),
        ]

        let sections = RemoteLibraryModel.groupByDay(assets, now: now, calendar: calendar)

        XCTAssertEqual(sections.map(\.title), ["今天", "昨天"])
        XCTAssertEqual(sections[0].assets.map(\.id), ["a", "b"], "同一天内应保持服务器返回顺序")
        XCTAssertEqual(sections[1].assets.map(\.id), ["c"])
        XCTAssertEqual(sections.map(\.count), [2, 1])
    }

    func testSameYearUsesMonthDayTitle() {
        let now = date(2026, 11, 1, 12, 0)
        let assets = [
            asset("a", taken: wallString(date(2026, 3, 5, 10, 0))),
        ]

        let sections = RemoteLibraryModel.groupByDay(assets, now: now, calendar: calendar)

        XCTAssertEqual(sections.count, 1)
        XCTAssertEqual(sections[0].title, "3月5日", "同年日期不带年份")
    }

    func testCrossYearTitleIncludesYear() {
        let now = date(2026, 1, 2, 10, 0)
        let assets = [
            asset("a", taken: wallString(date(2025, 12, 31, 22, 0))),
        ]

        let sections = RemoteLibraryModel.groupByDay(assets, now: now, calendar: calendar)

        XCTAssertEqual(sections[0].title, "2025年12月31日", "跨年日期要带年份")
    }

    func testNilTakenGoesToUnknownLast() {
        let now = date(2026, 11, 1, 12, 0)
        let assets = [
            asset("no-date", taken: nil),
            asset("today", taken: wallString(date(2026, 11, 1, 9, 0))),
            asset("old", taken: wallString(date(2020, 6, 18, 9, 0))),
        ]

        let sections = RemoteLibraryModel.groupByDay(assets, now: now, calendar: calendar)

        XCTAssertEqual(sections.count, 3)
        XCTAssertEqual(sections.last?.id, "unknown")
        XCTAssertEqual(sections.last?.title, "未知时间")
        XCTAssertEqual(sections.last?.assets.map(\.id), ["no-date"], "taken 为 nil 的固定放最后")
        XCTAssertEqual(sections.first?.title, "今天")
    }

    func testSectionsAreOrderedNewestFirst() {
        let now = date(2026, 11, 1, 12, 0)
        let assets = [
            asset("a", taken: wallString(date(2026, 10, 1, 9, 0))),
            asset("b", taken: wallString(date(2026, 10, 20, 9, 0))),
            asset("c", taken: wallString(date(2026, 10, 1, 8, 0))),
        ]

        let sections = RemoteLibraryModel.groupByDay(assets, now: now, calendar: calendar)

        XCTAssertEqual(sections.map(\.id), ["2026-10-20", "2026-10-01"])
        XCTAssertEqual(sections[1].assets.map(\.id), ["a", "c"], "同一天内保持原顺序")
    }

    // MARK: - 搜索词规范化（服务端 q= 参数）

    func testSearchQueryNormalization() {
        XCTAssertNil(RemoteLibraryModel.normalizedQuery(""))
        XCTAssertNil(RemoteLibraryModel.normalizedQuery("   \n "), "纯空白等于不过滤")
        XCTAssertEqual(RemoteLibraryModel.normalizedQuery("  IMG_0001  "), "IMG_0001", "首尾空白应剔除，词本身保留")
    }
}
