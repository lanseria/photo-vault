//
//  PhotoVaultSyncApp.swift
//  PhotoVaultSync
//
//  App 入口。整个 App 的状态根是 AppModel（App/AppModel.swift），
//  这里只负责挂环境对象 + 冷启动/回前台的生命周期钩子。
//

import SwiftUI

@main
struct PhotoVaultSyncApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @Environment(\.scenePhase) private var scenePhase
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .environment(model.settings)
                .tint(Theme.accent)
                .preferredColorScheme(.dark)
        }
        .onChange(of: scenePhase) { _, phase in
            model.handleScenePhase(phase)
        }
    }
}
