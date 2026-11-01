//
//  DesignSystem.swift
//  PhotoVaultSync
//
//  UI 共用的小组件：背景、卡片、页头、统计格、进度条、chip、按钮样式、触感。
//  基础颜色/圆角 token 在 Core/Theme.swift（琥珀强调 + 深色渐变），这里只做组合，
//  不放任何业务逻辑，方便三个页面保持一致的手感。
//

import SwiftUI
import UIKit

// MARK: - 触感反馈

@MainActor
enum Haptics {
    static func tap() { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
    static func success() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
    static func warning() { UINotificationFeedbackGenerator().notificationOccurred(.warning) }
    static func error() { UINotificationFeedbackGenerator().notificationOccurred(.error) }
}

// MARK: - 页面背景

/// 深色渐变 + 顶部一点琥珀色辉光，替代系统默认的纯黑，让层次更像「控制台」。
struct VaultBackground: View {
    var body: some View {
        ZStack {
            Theme.backgroundGradient
            RadialGradient(
                colors: [Theme.accent.opacity(0.14), Theme.accent.opacity(0.03), .clear],
                center: UnitPoint(x: 0.5, y: -0.08),
                startRadius: 8,
                endRadius: 540
            )
        }
        .ignoresSafeArea()
    }
}

// MARK: - 卡片

struct VaultCard<Content: View>: View {
    var padding: CGFloat = 16
    var fill: Color = Color.white.opacity(0.055)
    @ViewBuilder var content: () -> Content

    var body: some View {
        content()
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(padding)
            .background(
                RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
                    .fill(fill)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
                    .strokeBorder(
                        LinearGradient(
                            colors: [Color.white.opacity(0.17), Color.white.opacity(0.035)],
                            startPoint: .topLeading,
                            endPoint: .bottomTrailing
                        ),
                        lineWidth: 1
                    )
            )
            .shadow(color: .black.opacity(0.28), radius: 14, x: 0, y: 8)
    }
}

// MARK: - 字体 / 页头

/// 小号字距很开的大写标签（页头上方那一行）
struct Eyebrow: View {
    let text: String
    var color: Color = Theme.accent

    var body: some View {
        Text(text)
            .font(.system(size: 10, weight: .semibold, design: .rounded))
            .tracking(2.2)
            .foregroundStyle(color)
    }
}

/// 三个页面共用的页头：eyebrow + 大标题 + 说明 + 右侧附加内容
struct PageHeader<Trailing: View>: View {
    let eyebrow: String
    let title: String
    var subtitle: String?
    var subtitleColor: Color = .secondary
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Eyebrow(text: eyebrow)
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(title)
                    .font(.system(.largeTitle, design: .rounded, weight: .bold))
                Spacer(minLength: 8)
                trailing()
            }
            if let subtitle, !subtitle.isEmpty {
                Text(subtitle)
                    .font(.footnote)
                    .foregroundStyle(subtitleColor)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.top, 10)
        .padding(.bottom, 4)
        .padding(.horizontal, 2)
    }
}

extension PageHeader where Trailing == EmptyView {
    init(eyebrow: String, title: String, subtitle: String? = nil, subtitleColor: Color = .secondary) {
        self.init(eyebrow: eyebrow, title: title, subtitle: subtitle,
                  subtitleColor: subtitleColor, trailing: { EmptyView() })
    }
}

// MARK: - 统计格

struct StatTile: View {
    let value: String
    let label: String
    var tint: Color = .primary
    var alignment: HorizontalAlignment = .leading

    var body: some View {
        VStack(alignment: alignment, spacing: 3) {
            Text(value)
                .font(.system(.title3, design: .rounded, weight: .semibold))
                .monospacedDigit()
                .foregroundStyle(tint)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: alignment == .leading ? .leading : .trailing)
    }
}

/// 很细的竖分隔线
struct VaultDivider: View {
    var vertical = true
    var body: some View {
        Group {
            if vertical {
                Rectangle().fill(Color.white.opacity(0.09)).frame(width: 1)
            } else {
                Rectangle().fill(Color.white.opacity(0.09)).frame(height: 1)
            }
        }
    }
}

// MARK: - 进度

/// 进度条。fraction == nil 时显示流动的不确定进度（扫描阶段）。
struct VaultProgressBar: View {
    let fraction: Double?
    var tint: Color = Theme.accent
    var height: CGFloat = 8
    @State private var shimmer: CGFloat = -0.5

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.08))
                if let fraction {
                    Capsule()
                        .fill(LinearGradient(colors: [tint.opacity(0.78), tint],
                                             startPoint: .leading, endPoint: .trailing))
                        .frame(width: max(height, geo.size.width * CGFloat(min(max(fraction, 0), 1))))
                        .animation(.easeOut(duration: 0.35), value: fraction)
                } else {
                    Capsule()
                        .fill(LinearGradient(colors: [.clear, tint.opacity(0.85), .clear],
                                             startPoint: .leading, endPoint: .trailing))
                        .frame(width: geo.size.width * 0.42)
                        .offset(x: shimmer * geo.size.width)
                        .onAppear {
                            shimmer = -0.5
                            withAnimation(.linear(duration: 1.25).repeatForever(autoreverses: false)) {
                                shimmer = 1.15
                            }
                        }
                }
            }
        }
        .frame(height: height)
        .clipShape(Capsule())
        .accessibilityHidden(true)
    }
}

/// 圆环进度（本机统计用）
struct ProgressRing: View {
    let fraction: Double
    var tint: Color = Theme.accent
    var lineWidth: CGFloat = 6

    var body: some View {
        ZStack {
            Circle().stroke(Color.white.opacity(0.09), lineWidth: lineWidth)
            Circle()
                .trim(from: 0, to: max(0.001, min(1, fraction)))
                .stroke(
                    AngularGradient(colors: [tint.opacity(0.45), tint, tint.opacity(0.75)],
                                    center: .center),
                    style: StrokeStyle(lineWidth: lineWidth, lineCap: .round)
                )
                .rotationEffect(.degrees(-90))
                .animation(.easeInOut(duration: 0.5), value: fraction)
        }
        .accessibilityHidden(true)
    }
}

// MARK: - 状态点 / 徽标

struct StatusDot: View {
    let color: Color
    var pulsing = false
    @State private var animate = false

    var body: some View {
        ZStack {
            if pulsing {
                Circle()
                    .fill(color.opacity(0.35))
                    .frame(width: 9, height: 9)
                    .scaleEffect(animate ? 2.2 : 0.9)
                    .opacity(animate ? 0 : 0.9)
                    .onAppear {
                        withAnimation(.easeOut(duration: 1.3).repeatForever(autoreverses: false)) {
                            animate = true
                        }
                    }
            }
            Circle().fill(color).frame(width: 8, height: 8)
        }
        .frame(width: 14, height: 14)
        .accessibilityHidden(true)
    }
}

struct StatusBadge: View {
    let text: String
    let color: Color
    var pulsing = false

    var body: some View {
        HStack(spacing: 5) {
            StatusDot(color: color, pulsing: pulsing)
            Text(text)
                .font(.system(.caption2, design: .rounded, weight: .semibold))
                .foregroundStyle(color)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(Capsule().fill(color.opacity(0.12)))
        .overlay(Capsule().strokeBorder(color.opacity(0.28), lineWidth: 1))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(text)
    }
}

// MARK: - chip

/// 只读摘要 chip（同步范围等）
struct VaultChip: View {
    let icon: String
    let text: String
    var tint: Color = Color.white.opacity(0.85)

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: icon).font(.system(size: 10, weight: .semibold))
            Text(text).font(.system(.caption, design: .rounded, weight: .medium))
        }
        .foregroundStyle(tint)
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Capsule().fill(Color.white.opacity(0.06)))
        .overlay(Capsule().strokeBorder(Color.white.opacity(0.09), lineWidth: 1))
        .accessibilityElement(children: .combine)
        .accessibilityLabel(text)
    }
}

/// 可点选的筛选 chip
struct FilterChip: View {
    let text: String
    var selected: Bool
    var action: () -> Void

    var body: some View {
        Button {
            Haptics.tap()
            action()
        } label: {
            Text(text)
                .font(.system(.caption, design: .rounded, weight: .semibold))
                .foregroundStyle(selected ? Color.black.opacity(0.9) : Color.white.opacity(0.72))
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .background(
                    Capsule().fill(selected ? Theme.accent : Color.white.opacity(0.07))
                )
                .overlay(
                    Capsule().strokeBorder(selected ? Color.clear : Color.white.opacity(0.10), lineWidth: 1)
                )
        }
        .buttonStyle(.plain)
        .accessibilityLabel(text)
        .accessibilityAddTraits(selected ? [.isSelected] : [])
    }
}

// MARK: - 键值行

struct InfoRow: View {
    let label: String
    let value: String
    var valueColor: Color = .primary

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .frame(width: 74, alignment: .leading)
            Text(value)
                .font(.footnote.weight(.medium))
                .foregroundStyle(valueColor)
                .multilineTextAlignment(.trailing)
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
    }
}

// MARK: - 空状态

struct EmptyHint: View {
    let icon: String
    let title: String
    let message: String

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 26, weight: .light))
                .foregroundStyle(Theme.accent.opacity(0.8))
            Text(title).font(.subheadline.weight(.medium))
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 16)
    }
}

// MARK: - 引导卡（未配置 / 未授权 / 部分授权 / 有失败）

struct GuidanceCard: View {
    let icon: String
    let tint: Color
    let title: String
    let message: String
    var primaryTitle: String?
    var primaryAction: (() -> Void)?

    var body: some View {
        VaultCard {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 10) {
                    Image(systemName: icon)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(tint)
                        .frame(width: 32, height: 32)
                        .background(Circle().fill(tint.opacity(0.14)))
                    Text(title)
                        .font(.system(.subheadline, design: .rounded, weight: .semibold))
                    Spacer(minLength: 0)
                }
                Text(message)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let primaryTitle, let primaryAction {
                    Button(action: primaryAction) {
                        Label(primaryTitle, systemImage: "arrow.right.circle.fill")
                    }
                    .buttonStyle(VaultActionButtonStyle(variant: .secondary, tint: tint))
                    .accessibilityLabel(primaryTitle)
                }
            }
        }
    }
}

// MARK: - 按钮样式

struct VaultActionButtonStyle: ButtonStyle {
    enum Variant { case primary, secondary, danger }

    var variant: Variant = .primary
    var tint: Color = Theme.accent

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(.subheadline, design: .rounded, weight: .semibold))
            .lineLimit(1)
            .minimumScaleFactor(0.8)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 13)
            .padding(.horizontal, 12)
            .foregroundStyle(foreground)
            .background(background(pressed: configuration.isPressed))
            .overlay(border)
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
            .opacity(configuration.isPressed ? 0.92 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }

    private var foreground: Color {
        switch variant {
        case .primary: return Color.black.opacity(0.92)
        case .secondary: return Color.white.opacity(0.9)
        case .danger: return Theme.danger
        }
    }

    private func background(pressed: Bool) -> AnyShapeStyle {
        switch variant {
        case .primary:
            return AnyShapeStyle(
                LinearGradient(colors: [tint, tint.opacity(0.82)], startPoint: .top, endPoint: .bottom)
            )
        case .secondary:
            return AnyShapeStyle(Color.white.opacity(pressed ? 0.14 : 0.075))
        case .danger:
            return AnyShapeStyle(Theme.danger.opacity(pressed ? 0.2 : 0.12))
        }
    }

    private var border: some View {
        RoundedRectangle(cornerRadius: 13, style: .continuous)
            .strokeBorder(borderColor, lineWidth: 1)
    }

    private var borderColor: Color {
        switch variant {
        case .primary: return Color.white.opacity(0.22)
        case .secondary: return Color.white.opacity(0.16)
        case .danger: return Theme.danger.opacity(0.4)
        }
    }
}

/// 小号胶囊按钮（「测试连接」「调整」「复制」这类次要操作）
struct VaultMiniButtonStyle: ButtonStyle {
    var tint: Color = Color.white.opacity(0.85)

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(.caption, design: .rounded, weight: .semibold))
            .foregroundStyle(tint)
            .padding(.horizontal, 11)
            .padding(.vertical, 6)
            .background(Capsule().fill(Color.white.opacity(configuration.isPressed ? 0.16 : 0.08)))
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.10), lineWidth: 1))
            .opacity(configuration.isPressed ? 0.85 : 1)
    }
}

// MARK: - 时间文案

enum TimeText {
    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "zh_Hans_CN")
        f.dateFormat = "HH:mm:ss"
        return f
    }()

    private static let shortFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "zh_Hans_CN")
        f.dateFormat = "M月d日 HH:mm"
        return f
    }()

    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "zh_Hans_CN")
        f.dateFormat = "yyyy年M月d日"
        return f
    }()

    static func clock(_ date: Date) -> String { clockFormatter.string(from: date) }

    static func day(_ date: Date) -> String { dayFormatter.string(from: date) }

    static func relative(_ date: Date) -> String {
        let delta = Date().timeIntervalSince(date)
        if delta < 60 { return "刚刚" }
        if delta < 3600 { return "\(Int(delta / 60)) 分钟前" }
        let cal = Calendar.current
        if cal.isDateInToday(date) { return "今天 \(clockShort(date))" }
        if cal.isDateInYesterday(date) { return "昨天 \(clockShort(date))" }
        return shortFormatter.string(from: date)
    }

    private static func clockShort(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "zh_Hans_CN")
        f.dateFormat = "HH:mm"
        return f.string(from: date)
    }
}

// MARK: - 资产类型文案

enum AssetKind {
    static func label(_ kind: String) -> String {
        switch kind {
        case "photo": return "照片"
        case "video": return "视频"
        case "live_photo": return "实况照片"
        case "raw": return "RAW"
        default: return "其他"
        }
    }

    static func icon(_ kind: String) -> String {
        switch kind {
        case "video": return "video.fill"
        case "live_photo": return "livephoto"
        case "raw": return "camera.aperture"
        case "photo": return "photo.fill"
        default: return "doc.fill"
        }
    }
}

// MARK: - 页面通用外框

extension View {
    /// 统一的内边距 + 底部留白，配合 VaultBackground 使用
    func vaultPagePadding() -> some View {
        self
            .padding(.horizontal, Theme.hPadding)
            // iOS 26 的浮动 Tab 栏会盖住页面底部：这里留出 Tab 栏高度 + 呼吸空间，
            // 保证最后一张卡片能完整滚出来（三个页面共用）。
            .padding(.bottom, 120)
    }
}
