/** 浏览器能接住的媒体扩展名；后端还会再校验一次（meta.is_media）。 */
export const UPLOAD_EXT =
  /\.(jpe?g|jfif|jpe|png|heic|heif|webp|tiff?|bmp|gif|avif|dng|cr2|cr3|nef|arw|raf|orf|rw2|pef|srw|mp4|mov|m4v|avi|mkv|insv|360|lrv|3gp|mpeg|mpg|flv|wmv|mts|m2ts)$/i

export interface DroppedFile {
  file: File
  /** 拖文件夹进来时保留相对路径，入库后 origin 更好认 */
  name: string
}

export function newBatchId(): string {
  try {
    return crypto.randomUUID().replace(/-/g, '')
  } catch {
    return Date.now().toString(36) + Math.random().toString(36).slice(2)
  }
}
