import { expect, test } from '@playwright/test'

/**
 * SQL 模板参数绑定与数据预览取样的端到端验收：
 * - 绑定结果可执行、占位符逐项对应；
 * - 显式空列表被拦截，不回退默认值扩大查询；
 * - 日期/整数非法时按字段报错；
 * - 数据预览先绑定后取样，零行空态与绑定失败可区分；
 * - 日期在前、多城市在后时取样使用展开后的条件。
 */
test.describe('SQL模板参数绑定', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/sql_template')
    await page.waitForTimeout(1500)
    await expect(page.locator('tbody tr[data-testid^="row-"]')).toHaveCount(4)
  })

  test('合法参数生成可执行绑定结果，列表逐项展开', async ({ page }) => {
    await page.getByTestId('open-binding-1').click()
    await expect(page.getByTestId('binding-modal')).toBeVisible()
    await expect(page.getByTestId('binding-input-start_date')).toBeVisible()

    await page.getByTestId('binding-input-start_date').fill('2026-11-10')
    await page.getByTestId('binding-input-end_date').fill('2026-11-20')
    await page.getByTestId('binding-input-shop_list').fill('101,102,103')
    await page.getByTestId('submit-binding').click()

    await expect(page.getByTestId('binding-success')).toBeVisible()
    const sql = await page.getByTestId('binding-sql').innerText()
    expect(sql).toContain('BETWEEN $1 AND $2')
    expect(sql).toContain('IN ($3, $4, $5)')
  })

  test('清空城市列表触发空集合拦截，不回退默认值', async ({ page }) => {
    await page.getByTestId('open-binding-1').click()
    await page.getByTestId('binding-input-start_date').fill('2026-11-01')
    await page.getByTestId('binding-input-end_date').fill('2026-11-15')
    // 显式声明传空列表，模拟「选好日期却清空城市」
    await page.getByTestId('binding-empty-shop_list').check()
    await page.getByTestId('submit-binding').click()

    const errors = page.getByTestId('binding-errors')
    await expect(errors).toBeVisible()
    await expect(errors).toContainText('shop_list')
    await expect(errors).toContainText('空集合')
    await expect(page.getByTestId('binding-success')).toHaveCount(0)
  })

  test('非法日期按字段指出且绑定不可执行', async ({ page }) => {
    await page.getByTestId('open-binding-2').click() // SQL-USER-001: query_date:date
    await page.getByTestId('binding-input-query_date').fill('2026-02-31')
    await page.getByTestId('submit-binding').click()

    const errors = page.getByTestId('binding-errors')
    await expect(errors).toBeVisible()
    await expect(errors).toContainText('query_date')
    await expect(errors).toContainText('日期')
  })

  test('全部留空时回填模板自身声明的默认值', async ({ page }) => {
    await page.getByTestId('open-binding-1').click()
    await page.getByTestId('submit-binding').click()
    await expect(page.getByTestId('binding-success')).toBeVisible()
    const sql = await page.getByTestId('binding-sql').innerText()
    // 模板默认 shop_list=101,102
    expect(sql).toContain('IN ($3, $4)')
  })
})

test.describe('数据预览取样', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/data_preview')
    await page.waitForTimeout(1500)
    await expect(page.locator('tbody tr[data-testid^="row-"]')).toHaveCount(3)
  })

  test('绑定通过后取样，样本仅含展开条件命中的城市', async ({ page }) => {
    await page.getByTestId('open-sample-1').click()
    await expect(page.getByTestId('sample-modal')).toBeVisible()

    await page.getByTestId('sample-input-start_date').fill('2026-11-15')
    await page.getByTestId('sample-input-end_date').fill('2026-11-15')
    await page.getByTestId('sample-input-shop_list').fill('101,102,103')
    await page.getByTestId('submit-sample').click()

    await expect(page.getByTestId('sample-success')).toBeVisible()
    const result = page.getByTestId('sample-result')
    await expect(result).toContainText('IN ($3, $4, $5)')
    // 取样表内 2026-11-15 三个城市各一行
    const sampleRows = result.locator('table tbody tr')
    await expect(sampleRows).toHaveCount(3)
    const text = await result.innerText()
    expect(text).not.toContain('2026-11-14')
  })

  test('选好日期但清空城市：取样前拦截，不扫全城市', async ({ page }) => {
    await page.getByTestId('open-sample-1').click()
    await page.getByTestId('sample-input-start_date').fill('2026-11-01')
    await page.getByTestId('sample-input-end_date').fill('2026-11-15')
    // 显式勾选空集合：模拟「选好日期却清空城市」
    await page.getByTestId('sample-empty-shop_list').check()
    await page.getByTestId('submit-sample').click()

    const failed = page.getByTestId('sample-binding-failed')
    await expect(failed).toBeVisible()
    await expect(failed).toContainText('shop_list')
    await expect(page.getByTestId('sample-result')).toHaveCount(0)
  })

  test('零行空态与绑定失败区分展示', async ({ page }) => {
    await page.getByTestId('open-sample-1').click()
    await page.getByTestId('sample-input-start_date').fill('2020-01-01')
    await page.getByTestId('sample-input-end_date').fill('2020-01-02')
    await page.getByTestId('sample-input-shop_list').fill('101')
    await page.getByTestId('submit-sample').click()

    await expect(page.getByTestId('sample-zero-rows')).toBeVisible()
    await expect(page.getByTestId('sample-binding-failed')).toHaveCount(0)
  })
})
