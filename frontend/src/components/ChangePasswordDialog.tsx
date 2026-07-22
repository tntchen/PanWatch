import { useState } from 'react'
import { Lock } from 'lucide-react'
import { authApi } from '@panwatch/api'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@panwatch/base-ui/components/ui/dialog'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'
import { Label } from '@panwatch/base-ui/components/ui/label'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

interface ChangePasswordDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

/** 修改密码弹窗：需校验旧密码（J10 安全债修复），改密成功后旧 token 由后端 pwd_at 机制吊销。 */
export default function ChangePasswordDialog({ open, onOpenChange }: ChangePasswordDialogProps) {
  const { toast } = useToast()
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [loading, setLoading] = useState(false)

  const reset = () => {
    setOldPassword('')
    setNewPassword('')
    setConfirmPassword('')
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!oldPassword || !newPassword) return
    if (newPassword.length < 6) {
      toast('新密码长度至少 6 位', 'error')
      return
    }
    if (newPassword !== confirmPassword) {
      toast('两次新密码不一致', 'error')
      return
    }
    setLoading(true)
    try {
      await authApi.changePassword({ old_password: oldPassword, new_password: newPassword })
      toast('密码已更新，下次请求需重新登录', 'success')
      onOpenChange(false)
      reset()
    } catch (err) {
      toast(err instanceof Error ? err.message : '修改失败', 'error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={v => { onOpenChange(v); if (!v) reset() }}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Lock className="w-4 h-4 text-primary" />
            修改密码
          </DialogTitle>
          <DialogDescription>修改成功后当前登录将失效，需要重新登录。</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <Label>旧密码</Label>
            <Input
              type="password"
              value={oldPassword}
              onChange={e => setOldPassword(e.target.value)}
              placeholder="请输入当前密码"
              autoFocus
            />
          </div>
          <div>
            <Label>新密码</Label>
            <Input
              type="password"
              value={newPassword}
              onChange={e => setNewPassword(e.target.value)}
              placeholder="至少 6 位"
            />
          </div>
          <div>
            <Label>确认新密码</Label>
            <Input
              type="password"
              value={confirmPassword}
              onChange={e => setConfirmPassword(e.target.value)}
              placeholder="再次输入新密码"
            />
          </div>
          <Button type="submit" className="w-full" disabled={loading || !oldPassword || !newPassword}>
            {loading ? (
              <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            ) : (
              '确认修改'
            )}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
