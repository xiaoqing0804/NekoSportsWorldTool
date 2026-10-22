//! 中文字体安装。
//!
//! Linux 和 Android 使用仓库内置的 Noto Sans SC，避免依赖 Windows 字体路径。
//! 其他桌面平台依次尝试 msyh.ttc / msyh.ttf / simhei.ttf / simsun.ttc；
//! 全部失败返回 None，界面中文可能显示为方块。

#[cfg(not(any(target_os = "android", target_os = "linux")))]
use std::path::PathBuf;

#[cfg(any(target_os = "android", target_os = "linux"))]
const BUNDLED_FONT: &[u8] = include_bytes!("../../assets/fonts/NotoSansSC-VF.ttf");

/// 字体候选（按优先级）。
#[cfg(not(any(target_os = "android", target_os = "linux")))]
const CANDIDATES: [&str; 4] = ["msyh.ttc", "msyh.ttf", "simhei.ttf", "simsun.ttc"];

#[cfg(not(any(target_os = "android", target_os = "linux")))]
fn fonts_dir() -> PathBuf {
    std::env::var("WINDIR")
        .map(|w| PathBuf::from(w).join("Fonts"))
        .unwrap_or_else(|_| PathBuf::from(r"C:\Windows\Fonts"))
}

/// 安装中文字体到 egui。返回实际加载的字体名（None = 全部失败）。
pub fn install(ctx: &egui::Context) -> Option<String> {
    #[cfg(any(target_os = "android", target_os = "linux"))]
    {
        let name = "Noto Sans SC".to_owned();
        let mut fonts = egui::FontDefinitions::default();
        fonts
            .font_data
            .insert(name.clone(), egui::FontData::from_static(BUNDLED_FONT));
        if let Some(list) = fonts.families.get_mut(&egui::FontFamily::Proportional) {
            list.insert(0, name.clone());
        }
        if let Some(list) = fonts.families.get_mut(&egui::FontFamily::Monospace) {
            list.push(name.clone());
        }
        ctx.set_fonts(fonts);
        return Some(name);
    }

    #[cfg(not(any(target_os = "android", target_os = "linux")))]
    {
    let dir = fonts_dir();
    for name in CANDIDATES {
        let path = dir.join(name);
        let Ok(data) = std::fs::read(&path) else {
            continue;
        };
        let mut fonts = egui::FontDefinitions::default();
        // ttc 集合取 index 0（msyh.ttc[0] = Microsoft YaHei）
        fonts
            .font_data
            .insert(name.to_string(), egui::FontData::from_owned(data));
        // proportional：插到最前（中文优先由雅黑渲染）
        if let Some(list) = fonts.families.get_mut(&egui::FontFamily::Proportional) {
            list.insert(0, name.to_string());
        }
        // monospace：追加（数字/ASCII 仍用默认等宽，中文回落雅黑）
        if let Some(list) = fonts.families.get_mut(&egui::FontFamily::Monospace) {
            list.push(name.to_string());
        }
        ctx.set_fonts(fonts);
        return Some(name.to_string());
    }
    None
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    #[test]
    fn chinese_ui_text_has_glyphs_in_both_font_families() {
        let ctx = egui::Context::default();
        super::install(&ctx);
        let _ = ctx.run(egui::RawInput::default(), |ctx| {
            ctx.fonts(|fonts| {
                for family in [egui::FontFamily::Proportional, egui::FontFamily::Monospace] {
                    let font = egui::FontId::new(14.0, family);
                    for ch in "校园跑登录设置ABC012".chars() {
                        assert!(fonts.has_glyph(&font, ch), "missing glyph {ch} in {font:?}");
                    }
                }
            });
        });
    }
}
