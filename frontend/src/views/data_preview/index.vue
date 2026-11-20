<template>
  <section class="page" data-module="data_preview">
    <header class="page-head">
      <div>
        <h2>数据预览管理</h2>
        <p class="page-desc">维护数据接口的预查询模板、采样配置与结果缓存管理，为数据预览提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记预览模板</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出数据预览清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`open-sample-${row.id}`"
              @click="openSample(row)"
            >
              取样预览
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据预览数据，可先登记预览模板</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记预览模板</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="showSample" class="modal-backdrop" data-testid="sample-modal">
      <form class="modal" @submit.prevent="submitSample">
        <h3>数据预览取样 - {{ sampleRow?.['模板编号'] }}</h3>
        <p class="page-desc">
          先完成模板参数绑定再取样；标量留空走模板默认值，列表清空提交会因必填条件冲突被拦截。
        </p>
        <label v-for="param in sampleParams" :key="param.name" class="create-field">
          <span>{{ param.name }}（{{ param.type }}）<em v-if="param.has_default">默认：{{ param.default }}</em></span>
          <input
            v-model="sampleValues[param.name]"
            :data-testid="`sample-input-${param.name}`"
            :disabled="sampleEmpty[param.name] === true"
            :placeholder="param.type === 'list' ? '多个值用英文逗号分隔；留空且不勾选空集合时走默认值' : ''"
          />
          <label v-if="param.type === 'list'" class="empty-toggle">
            <input
              type="checkbox"
              :data-testid="`sample-empty-${param.name}`"
              v-model="sampleEmpty[param.name]"
            />
            显式传空列表（空集合，不回填默认值，必填条件将被拦截）
          </label>
        </label>

        <div v-if="sampleError" class="binding-result" data-testid="sample-binding-failed">
          <p class="error-text">绑定校验失败，未执行查询（状态：{{ sampleError.binding_status ?? 'binding_failed' }}）</p>
          <ul class="error-list">
            <li v-for="(message, field) in sampleError.field_errors" :key="field" class="error-text">
              【{{ field }}】{{ message }}
            </li>
          </ul>
        </div>

        <div v-if="sampleResult" data-testid="sample-result">
          <p v-if="sampleResult.status === 'executed_zero_rows'" class="warn-text" data-testid="sample-zero-rows">
            查询已执行，返回零行空态（绑定合法，仅无匹配数据）
          </p>
          <p v-else class="ok-text" data-testid="sample-success">
            取样成功，共 {{ sampleResult.row_count }} 行（上限 {{ sampleResult.sample_limit }}）
          </p>
          <p class="page-desc">展开SQL：{{ sampleResult.bound_sql }}</p>
          <ul class="binding-conditions">
            <li v-for="cond in sampleResult.applied_conditions" :key="cond" class="page-desc">{{ cond }}</li>
          </ul>
          <table v-if="sampleResult.samples.length" class="data-table sample-table">
            <thead>
              <tr><th v-for="key in sampleColumns" :key="key">{{ key }}</th></tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in sampleResult.samples" :key="index">
                <td v-for="key in sampleColumns" :key="key">{{ row[key] ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="page-actions">
          <button class="btn" type="button" @click="showSample = false">关闭</button>
          <button class="btn primary" data-testid="submit-sample" type="submit" :disabled="sampleLoading">
            {{ sampleLoading ? '取样中…' : '绑定并取样' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据预览记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface SampleParam {
  name: string
  type: string
  default: string
  has_default: boolean
}

interface SampleResult {
  status: string
  row_count: number
  sample_limit: number
  bound_sql: string
  applied_conditions: string[]
  samples: Array<Record<string, string | number>>
}

interface SampleFailure {
  binding_status?: string
  field_errors: Record<string, string>
  message?: string
}

const ENDPOINT = '/api/data_preview'
const columns = ["模板编号", "数据源名", "预查询SQL", "返回列数", "采样行数", "缓存秒数", "刷新策略", "模板版本"]
const actions = ["提交校验", "启用模板", "停用模板"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["预览模板总数", "待校验模板", "已启用模板"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const showSample = ref(false)
const sampleLoading = ref(false)
const sampleRow = ref<Row | null>(null)
const sampleParams = ref<SampleParam[]>([])
const sampleValues = ref<Record<string, string>>({})
const sampleEmpty = ref<Record<string, boolean>>({})
const sampleResult = ref<SampleResult | null>(null)
const sampleError = ref<SampleFailure | null>(null)
const sampleColumns = computed(() => {
  const first = sampleResult.value?.samples[0]
  return first ? Object.keys(first) : []
})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'data_preview.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据预览动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据预览操作失败'
  }
}

async function openSample(row: Row) {
  errorMessage.value = ''
  sampleResult.value = null
  sampleError.value = null
  sampleRow.value = row
  sampleParams.value = []
  sampleValues.value = {}
  showSample.value = true
  try {
    const response = await request(`${ENDPOINT}/${row.id}/sample-params`)
    if (!response.ok) throw new Error('取样参数声明读取失败')
    const payload = await response.json()
    sampleParams.value = payload.params ?? []
    sampleValues.value = Object.fromEntries(
      sampleParams.value.map((param) => [param.name, '']),
    )
    sampleEmpty.value = Object.fromEntries(
      sampleParams.value.map((param) => [param.name, false]),
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '取样参数声明读取失败'
    showSample.value = false
  }
}

async function submitSample() {
  if (!sampleRow.value) return
  sampleLoading.value = true
  sampleResult.value = null
  sampleError.value = null
  errorMessage.value = ''
  try {
    const values: Record<string, string | string[]> = {}
    for (const param of sampleParams.value) {
      const raw = sampleValues.value[param.name] ?? ''
      if (param.type === 'list') {
        if (sampleEmpty.value[param.name]) {
          // 调用方明确传空集合：后端在取样前拦截，绝不扩大为全表查询
          values[param.name] = []
        } else if (raw !== '') {
          values[param.name] = raw.split(',').map((item) => item.trim()).filter(Boolean)
        }
        // 留空且未勾选空集合 => 键缺省，回填模板自身声明的默认值
      } else if (raw !== '') {
        values[param.name] = raw
      }
    }
    const response = await request(`${ENDPOINT}/${sampleRow.value.id}/samples`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!response.ok) {
      // 422：绑定不可执行，detail 内带字段级错误，不产生任何样本
      sampleError.value = payload.detail as SampleFailure
      return
    }
    sampleResult.value = payload as SampleResult
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据预览取样请求失败'
  } finally {
    sampleLoading.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('预览模板列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据预览列表读取失败'
  }
}

onMounted(reload)
</script>
