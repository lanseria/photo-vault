//
//  Theme.swift
//  PhotoVaultSync
//
//  一套很轻的视觉常量：深色底 + 琥珀色强调。UI 组件直接用，不再散落魔法数字。
//

import SwiftUI

enum Theme {
    static let accent = Color(red: 1.00, green: 0.58, blue: 0.16)
    static let accentSoft = Color(red: 1.00, green: 0.72, blue: 0.36)

    static let success = Color(red: 0.20, green: 0.78, blue: 0.51)
    static let warning = Color(red: 1.00, green: 0.72, blue: 0.20)
    static let danger = Color(red: 0.95, green: 0.36, blue: 0.36)

    /// 卡片圆角
    static let radius: CGFloat = 18
    /// 页面左右留白
    static let hPadding: CGFloat = 18

    /// 深色渐变背景（浅色模式下也保持"监视器"的感觉）
    static let backgroundGradient = LinearGradient(
        colors: [
            Color(red: 0.055, green: 0.070, blue: 0.098),
            Color(red: 0.082, green: 0.098, blue: 0.133),
        ],
        startPoint: .top,
        endPoint: .bottom
    )

    static let cardFill = Color.white.opacity(0.06)
    static let cardStroke = Color.white.opacity(0.10)

    static func phaseColor(_ phase: SyncPhase) -> Color {
        switch phase {
        case .idle: return .secondary
        case .scanning, .syncing: return accent
        case .paused: return warning
        case .finished: return success
        case .failed: return danger
        }
    }

    static func phaseIcon(_ phase: SyncPhase) -> String {
        switch phase {
        case .idle: return "moon.zzz"
        case .scanning: return "magnifyingglass"
        case .syncing: return "arrow.up.circle"
        case .paused: return "pause.circle"
        case .finished: return "checkmark.circle"
        case .failed: return "exclamationmark.triangle"
        }
    }
}

/// 半透明卡片
struct CardBackground: ViewModifier {
    var padding: CGFloat = 16
    func body(content: Content) -> some View {
        content
            .padding(padding)
            .background(
                RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
                    .fill(Theme.cardFill)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.radius, style: .continuous)
                    .strokeBorder(Theme.cardStroke, lineWidth: 1)
            )
    }
}

extension View {
    func card(padding: CGFloat = 16) -> some View {
        modifier(CardBackground(padding: padding))
    }
}
