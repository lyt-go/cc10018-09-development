<template>
  <section class="page" data-module="sql_template">
    <header class="page-head">
      <div>
        <h2>SQL模板管理</h2>
        <p class="page-desc">维护可参数化的SQL查询模板注册与复用管理，为SQL模板提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记模板定义</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出SQL模板清单</button>
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
              :data-testid="`open-binding-${row.id}`"
              @click="openBinding(row)"
            >
              参数绑定
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无SQL模板数据，可先登记模板定义</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记模板定义</h3>
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

    <div v-if="showBinding" class="modal-backdrop" data-testid="binding-modal">
      <form class="modal" @submit.prevent="submitBinding">
        <h3>模板参数绑定 - {{ bindingRow?.['模板编号'] }}</h3>
        <p class="page-desc">留空的参数仅在模板声明了默认值时回填；列表参数清空提交会被判为空集合，不会扩大查询。</p>
        <label v-for="param in bindingParams" :key="param.name" class="create-field">
          <span>{{ param.name }}（{{ param.type }}）<em v-if="param.has_default">默认：{{ param.default }}</em></span>
          <input
            v-model="bindingValues[param.name]"
            :data-testid="`binding-input-${param.name}`"
            :disabled="bindingEmpty[param.name] === true"
            :placeholder="param.type === 'list' ? '多个值用英文逗号分隔；留空且不勾选空集合时走默认值' : ''"
          />
          <label v-if="param.type === 'list'" class="empty-toggle">
            <input
              type="checkbox"
              :data-testid="`binding-empty-${param.name}`"
              v-model="bindingEmpty[param.name]"
            />
            显式传空列表（空集合，不回填默认值，必填条件将被拦截）
          </label>
        </label>
        <div v-if="bindingResult" class="binding-result" data-testid="binding-result">
          <p v-if="bindingResult.ok" class="ok-text" data-testid="binding-success">
            绑定成功，SQL 可执行（状态：{{ bindingResult.status }}）
          </p>
          <ul v-else class="error-list" data-testid="binding-errors">
            <li v-for="(message, field) in bindingResult.field_errors" :key="field" class="error-text">
              【{{ field }}】{{ message }}
            </li>
          </ul>
          <template v-if="bindingResult.ok">
            <p class="binding-sql" data-testid="binding-sql">{{ bindingResult.sql }}</p>
            <p class="page-desc">实际参数：{{ JSON.stringify(bindingResult.params) }}</p>
          </template>
        </div>
        <div class="page-actions">
          <button class="btn" type="button" @click="showBinding = false">关闭</button>
          <button class="btn primary" data-testid="submit-binding" type="submit" :disabled="bindingLoading">
            {{ bindingLoading ? '绑定中…' : '生成绑定结果' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条SQL模板记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface BindingParam {
  name: string
  type: string
  default: string
  has_default: boolean
}

interface BindingResult {
  ok: boolean
  status: string
  sql: string | null
  params: Array<string | number>
  field_errors: Record<string, string>
}

const ENDPOINT = '/api/sql_template'
const columns = ["模板编号", "模板名称", "SQL片段", "参数标记", "参数类型", "默认值", "使用次数", "模板版本"]
const actions = ["提交校验", "标记生效", "标记废弃"]
const statuses = ["草稿", "待校验", "已生效", "已废弃"]
const metricNames = ["模板定义总数", "待校验模板", "已生效模板"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const showBinding = ref(false)
const bindingLoading = ref(false)
const bindingRow = ref<Row | null>(null)
const bindingParams = ref<BindingParam[]>([])
const bindingValues = ref<Record<string, string>>({})
const bindingEmpty = ref<Record<string, boolean>>({})
const bindingResult = ref<BindingResult | null>(null)
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
    link.download = 'sql_template.csv'
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
      throw new Error('SQL模板动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SQL模板操作失败'
  }
}

async function openBinding(row: Row) {
  errorMessage.value = ''
  bindingResult.value = null
  bindingRow.value = row
  bindingParams.value = []
  bindingValues.value = {}
  showBinding.value = true
  try {
    const response = await request(`${ENDPOINT}/${row.id}/binding-params`)
    if (!response.ok) throw new Error('模板参数声明读取失败')
    const payload = await response.json()
    bindingParams.value = payload.params ?? []
    bindingValues.value = Object.fromEntries(
      bindingParams.value.map((param) => [param.name, '']),
    )
    bindingEmpty.value = Object.fromEntries(
      bindingParams.value.map((param) => [param.name, false]),
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '模板参数声明读取失败'
    showBinding.value = false
  }
}

async function submitBinding() {
  if (!bindingRow.value) return
  bindingLoading.value = true
  bindingResult.value = null
  errorMessage.value = ''
  try {
    const values: Record<string, string | string[]> = {}
    for (const param of bindingParams.value) {
      const raw = bindingValues.value[param.name] ?? ''
      if (param.type === 'list') {
        if (bindingEmpty.value[param.name]) {
          // 调用方明确要求传空集合：显式发空列表，由后端按必填条件拦截，
          // 不能因为省略键而被模板默认值悄悄扩大查询。
          values[param.name] = []
        } else if (raw !== '') {
          values[param.name] = raw.split(',').map((item) => item.trim()).filter(Boolean)
        }
        // 留空且未勾选空集合 => 键缺省，回填模板自身声明的默认值
      } else if (raw !== '') {
        // 标量留空表示「未提供」，交由模板默认值回填
        values[param.name] = raw
      }
    }
    const response = await request(`${ENDPOINT}/${bindingRow.value.id}/binding`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    bindingResult.value = (await response.json()) as BindingResult
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '参数绑定请求失败'
  } finally {
    bindingLoading.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('模板定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SQL模板列表读取失败'
  }
}

onMounted(reload)
</script>
