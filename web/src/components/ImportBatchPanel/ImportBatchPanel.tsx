import { FileUp } from "lucide-react";
import type {
  ImportBatch,
  ImportStage,
  ImportTask,
} from "../../features/documents/types";
import { Button } from "../Button/Button";

type ImportTaskRowProps = {
  batchId: string;
  cancelling: boolean;
  onCancel: (batchId: string, taskId: string) => void;
  onRetry: (batchId: string, taskId: string) => void;
  retrying: boolean;
  task: ImportTask;
};

function taskState(task: ImportTask): "running" | "queued" | "failed" | "cancelled" {
  if (task.status === "running") return "running";
  if (task.status === "failed") return "failed";
  if (task.status === "cancelled") return "cancelled";
  return "queued";
}

export function importStageText(stage: ImportStage): string {
  if (stage === "queued") return "排队中";
  if (stage === "staged") return "文件已准备";
  if (stage === "parsing") return "正在解析文档";
  if (stage === "chunking") return "正在切分内容";
  if (stage === "embedding") return "正在生成索引";
  if (stage === "persisting") return "正在保存索引";
  if (stage === "committing") return "正在提交文档";
  if (stage === "succeeded") return "已完成";
  if (stage === "failed") return "导入失败";
  return "已取消";
}

function taskStatusText(task: ImportTask): string {
  if (task.cancel_requested_at) return "正在取消";
  if (task.status === "running") {
    return `${importStageText(task.stage)} · ${task.progress}%`;
  }
  if (task.status === "retry_wait") return `等待重试 · ${task.progress}%`;
  if (task.status === "queued") return importStageText(task.stage);
  if (task.status === "failed") return task.error_summary ?? "导入失败，请重试";
  if (task.status === "cancelled") return "已取消";
  return "已完成";
}

function taskBadgeText(task: ImportTask): string {
  if (task.cancel_requested_at) return "正在取消";
  if (task.status === "running") return "导入中";
  if (task.status === "retry_wait") return "等待重试";
  if (task.status === "queued") return "排队中";
  if (task.status === "failed") return "失败";
  if (task.status === "cancelled") return "已取消";
  return "已完成";
}

function isCancellable(task: ImportTask): boolean {
  return (
    (task.status === "queued" ||
      task.status === "running" ||
      task.status === "retry_wait") &&
    task.stage !== "committing" &&
    task.cancel_requested_at === null
  );
}

export function ImportTaskRow({
  batchId,
  cancelling,
  onCancel,
  onRetry,
  retrying,
  task,
}: ImportTaskRowProps) {
  const state = taskState(task);
  return (
    <li className="import-task-row" data-state={state} aria-label={task.original_name}>
      <span className="import-task-row__icon" aria-hidden="true">
        <FileUp size={18} strokeWidth={1.6} />
      </span>
      <span className="import-task-row__body">
        <strong>{task.original_name}</strong>
        <span>{taskStatusText(task)}</span>
        {task.status === "running" || task.status === "retry_wait" ? (
          <progress
            aria-label={`${task.original_name} 导入进度`}
            max={100}
            value={Math.max(0, Math.min(task.progress, 100))}
          />
        ) : null}
      </span>
      <span className="import-task-row__badge">{taskBadgeText(task)}</span>
      {task.status === "failed" ? (
        <Button
          className="document-action-target"
          size="sm"
          loading={retrying}
          aria-label={`重试 ${task.original_name}`}
          onClick={() => onRetry(batchId, task.task_id)}
        >
          重试
        </Button>
      ) : null}
      {isCancellable(task) ? (
        <Button
          className="document-action-target"
          hierarchy="secondary"
          size="sm"
          loading={cancelling}
          aria-label={`取消 ${task.original_name}`}
          onClick={() => onCancel(batchId, task.task_id)}
        >
          取消
        </Button>
      ) : null}
    </li>
  );
}

type ImportBatchPanelProps = {
  batches: ImportBatch[];
  cancellingTask?: { batchId: string; taskId: string };
  onCancel: (batchId: string, taskId: string) => void;
  onRetry: (batchId: string, taskId: string) => void;
  onRetryFailed: (batchId: string) => void;
  retryingBatchId?: string;
  retryingTask?: { batchId: string; taskId: string };
};

export function ImportBatchPanel({
  batches,
  cancellingTask,
  onCancel,
  onRetry,
  onRetryFailed,
  retryingBatchId,
  retryingTask,
}: ImportBatchPanelProps) {
  const visibleBatches = batches.filter(
    (batch) =>
      batch.counts.failed > 0 ||
      batch.tasks.some(
        (task) =>
          task.status === "queued" ||
          task.status === "running" ||
          task.status === "retry_wait",
      ),
  );
  if (!visibleBatches.length) return null;

  return (
    <div className="import-batches">
      {visibleBatches.map((batch) => {
        const visibleTasks = batch.tasks.filter((task) => task.status !== "succeeded");
        const active = batch.tasks.some((task) =>
          task.status === "queued" || task.status === "running" || task.status === "retry_wait",
        );
        const failed = batch.counts.failed > 0;
        return (
          <section
            className={`import-batch${failed ? " import-batch--failed" : ""}`}
            key={batch.batch_id}
            aria-label={active ? "导入任务" : "部分导入失败"}
          >
            <header className="import-batch__header">
              <div>
                <h2>
                  {active
                    ? failed
                      ? `正在导入，${batch.counts.failed} 个文件失败`
                      : "正在导入"
                    : `导入完成，${batch.counts.failed} 个文件失败`}
                </h2>
                <p>
                  完成 {batch.counts.succeeded}/{batch.counts.total}
                  {active ? " · 任务会自动刷新" : ""}
                </p>
              </div>
              {failed ? (
                <Button
                  loading={retryingBatchId === batch.batch_id}
                  onClick={() => onRetryFailed(batch.batch_id)}
                >
                  重试全部失败项
                </Button>
              ) : null}
            </header>
            {visibleTasks.length ? (
              <ul className="import-task-list" aria-label="导入任务列表">
                {visibleTasks.map((task) => (
                  <ImportTaskRow
                    key={task.task_id}
                    batchId={batch.batch_id}
                    task={task}
                    retrying={
                      retryingTask?.batchId === batch.batch_id &&
                      retryingTask.taskId === task.task_id
                    }
                    cancelling={
                      cancellingTask?.batchId === batch.batch_id &&
                      cancellingTask.taskId === task.task_id
                    }
                    onRetry={onRetry}
                    onCancel={onCancel}
                  />
                ))}
              </ul>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}
