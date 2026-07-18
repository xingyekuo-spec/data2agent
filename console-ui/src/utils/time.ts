/** 展示用时间格式化:输入带时区 ISO 字符串,输出本地 HH:mm;非法输入为空串 */
export function formatTimeHM(value: string | null | undefined): string {
  if (!value) {
    return ''
  }
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) {
    return ''
  }
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

/** 展示用日期时间:输出本地 MM-dd HH:mm;非法输入为空串 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return ''
  }
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) {
    return ''
  }
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}
