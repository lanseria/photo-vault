//
//  RootView.swift
//  PhotoVaultSync
//
//  三 Tab 主界面：同步 / 图库 / 设置。
//  深色 + 琥珀强调色；Tab 栏毛玻璃由 UITabBarAppearance 配置（iOS 26 也可能用
//  系统新的 Liquid Glass 外观，两种都保持深色可读）。
//

import SwiftUI
import UIKit

struct RootView: View {
    @Environment(AppModel.self) private var model

    init() {
        Self.configureTabBarAppearance()
    }

    var body: some View {
        @Bindable var model = model

        TabView(selection: $model.selectedTab) {
            SyncView()
                .tag(AppTab.sync)
                .tabItem {
                    Label("同步", systemImage: "arrow.triangle.2.circlepath")
                }

            LibraryView()
                .tag(AppTab.library)
                .tabItem {
                    Label("图库", systemImage: "square.grid.3x3.fill")
                }

            SettingsView()
                .tag(AppTab.settings)
                .tabItem {
                    Label("设置", systemImage: "gearshape.fill")
                }
        }
        .tint(Theme.accent)
        .task {
            await model.bootstrap()
        }
        .onChange(of: model.engine.state.phase) { oldPhase, newPhase in
            guard oldPhase != newPhase else { return }
            switch newPhase {
            case .finished:
                Haptics.success()
            case .failed:
                Haptics.error()
            case .paused:
                Haptics.warning()
            default:
                break
            }
        }
    }

    // MARK: - Tab 栏外观

    private static func configureTabBarAppearance() {
        let appearance = UITabBarAppearance()
        appearance.configureWithTransparentBackground()
        appearance.backgroundEffect = UIBlurEffect(style: .systemUltraThinMaterialDark)
        appearance.backgroundColor = UIColor(red: 0.045, green: 0.055, blue: 0.080, alpha: 0.62)
        appearance.shadowColor = UIColor.white.withAlphaComponent(0.08)

        let item = appearance.stackedLayoutAppearance
        item.normal.iconColor = UIColor.white.withAlphaComponent(0.38)
        item.normal.titleTextAttributes = [.foregroundColor: UIColor.white.withAlphaComponent(0.38)]
        item.selected.iconColor = UIColor(Theme.accent)
        item.selected.titleTextAttributes = [.foregroundColor: UIColor(Theme.accent)]

        UITabBar.appearance().standardAppearance = appearance
        UITabBar.appearance().scrollEdgeAppearance = appearance
    }
}

#Preview {
    RootView()
        .environment(AppModel())
        .environment(SettingsStore())
        .preferredColorScheme(.dark)
}
