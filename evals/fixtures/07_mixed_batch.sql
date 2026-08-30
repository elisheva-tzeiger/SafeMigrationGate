DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE status = 'cancelled');
UPDATE orders SET user_id = 0 WHERE status = 'cancelled';
TRUNCATE TABLE audit_log;
DROP TABLE archive_2019;
