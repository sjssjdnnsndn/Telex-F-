import { useState, useEffect } from 'react';
import { ShieldCheck, Save, QrCode } from 'lucide-react';
import { useUpdatePaymentInfo, getGetPaymentInfoQueryKey } from '@workspace/api-client-react';
import { useQueryClient } from '@tanstack/react-query';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { useToast } from '@/hooks/use-toast';

interface PaymentInfo {
  upiId: string;
  qrImageUrl: string;
  minDeposit: number;
}

/**
 * Shown only to the Telegram user matching the bot's admin id (verified
 * server-side from signed initData — see routes/upi.ts `isAdminId`). Lets the
 * admin update the UPI QR code, UPI ID, and minimum deposit that every user
 * sees on the deposit screen, without needing to touch the database directly.
 */
export default function AdminPanel({ paymentInfo }: { paymentInfo?: PaymentInfo }) {
  const [upiId, setUpiId] = useState('');
  const [qrImageUrl, setQrImageUrl] = useState('');
  const [minDeposit, setMinDeposit] = useState('');
  const { toast } = useToast();
  const queryClient = useQueryClient();

  useEffect(() => {
    if (paymentInfo) {
      setUpiId(paymentInfo.upiId);
      setQrImageUrl(paymentInfo.qrImageUrl);
      setMinDeposit(String(paymentInfo.minDeposit));
    }
  }, [paymentInfo]);

  const updateMutation = useUpdatePaymentInfo();

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    const parsedMinDeposit = Number(minDeposit);

    if (!upiId.trim() || !qrImageUrl.trim() || Number.isNaN(parsedMinDeposit) || parsedMinDeposit < 0) {
      toast({
        title: 'Invalid input',
        description: 'Please fill in a valid UPI ID, QR image URL, and minimum deposit.',
        variant: 'destructive',
      });
      return;
    }

    updateMutation.mutate(
      { data: { upiId: upiId.trim(), qrImageUrl: qrImageUrl.trim(), minDeposit: parsedMinDeposit } },
      {
        onSuccess: () => {
          toast({ title: 'Saved', description: 'Payment settings updated for all users.' });
          queryClient.invalidateQueries({ queryKey: getGetPaymentInfoQueryKey() });
        },
        onError: (err: any) => {
          const message = err?.response?.data?.error || err?.message || 'Failed to save settings.';
          toast({ title: 'Save failed', description: message, variant: 'destructive' });
        },
      },
    );
  };

  return (
    <Card className="p-5 border-2 border-primary/30 bg-primary/5">
      <h3 className="font-bold text-sm mb-1 text-foreground flex items-center gap-2">
        <ShieldCheck className="h-4 w-4 text-primary" />
        Admin Panel
      </h3>
      <p className="text-xs text-muted-foreground mb-4">
        Update the QR code and UPI ID shown to every user.
      </p>

      <form onSubmit={handleSave} className="flex flex-col gap-3">
        <div className="space-y-1.5">
          <label className="text-xs font-semibold text-foreground pl-1">UPI ID</label>
          <Input
            value={upiId}
            onChange={(e) => setUpiId(e.target.value)}
            placeholder="yourupi@bank"
            className="h-11 bg-white dark:bg-card text-foreground"
            data-testid="input-admin-upi-id"
          />
        </div>

        <div className="space-y-1.5">
          <label className="text-xs font-semibold text-foreground pl-1 flex items-center gap-1.5">
            <QrCode className="h-3.5 w-3.5" />
            QR Image URL
          </label>
          <Input
            value={qrImageUrl}
            onChange={(e) => setQrImageUrl(e.target.value)}
            placeholder="https://..."
            className="h-11 bg-white dark:bg-card text-foreground"
            data-testid="input-admin-qr-url"
          />
          {qrImageUrl ? (
            <img
              src={qrImageUrl}
              alt="QR preview"
              className="w-20 h-20 object-cover rounded-lg border mt-1"
              onError={(e) => (e.currentTarget.style.display = 'none')}
              onLoad={(e) => (e.currentTarget.style.display = 'block')}
            />
          ) : null}
        </div>

        <div className="space-y-1.5">
          <label className="text-xs font-semibold text-foreground pl-1">Minimum Deposit (₹)</label>
          <Input
            type="number"
            value={minDeposit}
            onChange={(e) => setMinDeposit(e.target.value)}
            placeholder="1"
            className="h-11 bg-white dark:bg-card text-foreground"
            data-testid="input-admin-min-deposit"
          />
        </div>

        <Button
          type="submit"
          size="lg"
          className="w-full mt-1"
          disabled={updateMutation.isPending}
          data-testid="button-admin-save"
        >
          <Save className="mr-2 h-4 w-4" />
          {updateMutation.isPending ? 'Saving...' : 'Save Settings'}
        </Button>
      </form>
    </Card>
  );
}
