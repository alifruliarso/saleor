import graphene
from django.core.exceptions import ValidationError

from ....account.models import User
from ....core.exceptions import InsufficientStock
from ....core.permissions import OrderPermissions
from ....core.taxes import zero_taxed_money
from ....core.tracing import traced_atomic_transaction
from ....order import OrderStatus, models
from ....order.actions import order_created
from ....order.error_codes import OrderErrorCode
from ....order.fetch import OrderInfo, OrderLineInfo
from ....order.search import prepare_order_search_document_value
from ....order.utils import get_order_country
from ....warehouse.management import allocate_preorders, allocate_stocks
from ....warehouse.reservations import is_reservation_enabled
from ...core.mutations import BaseMutation
from ...core.types import OrderError
from ..types import Order
from ..utils import (
    prepare_insufficient_stock_order_validation_errors,
    validate_draft_order,
)


class DraftOrderComplete(BaseMutation):
    order = graphene.Field(Order, description="Completed order.")

    class Arguments:
        id = graphene.ID(
            required=True, description="ID of the order that will be completed."
        )

    class Meta:
        description = "Completes creating an order."
        permissions = (OrderPermissions.MANAGE_ORDERS,)
        error_type_class = OrderError
        error_type_field = "order_errors"

    @classmethod
    def update_user_fields(cls, order):
        if order.user:
            order.user_email = order.user.email
        elif order.user_email:
            try:
                order.user = User.objects.get(email=order.user_email)
            except User.DoesNotExist:
                order.user = None

    @classmethod
    def validate_order(cls, order):
        if not order.is_draft():
            raise ValidationError(
                {
                    "id": ValidationError(
                        "The order is not draft.", code=OrderErrorCode.INVALID.value
                    )
                }
            )

    @classmethod
    def perform_mutation(cls, _root, info, id):
        manager = info.context.plugins
        order = cls.get_node_or_error(
            info,
            id,
            only_type=Order,
            qs=models.Order.objects.prefetch_related("lines__variant"),
        )
        cls.validate_order(order)

        country = get_order_country(order)
        validate_draft_order(order, country, info.context.plugins)
        cls.update_user_fields(order)
        order.status = OrderStatus.UNFULFILLED

        if not order.is_shipping_required():
            order.shipping_method_name = None
            order.shipping_price = zero_taxed_money(order.currency)
            if order.shipping_address:
                order.shipping_address.delete()
                order.shipping_address = None

        order.search_document = prepare_order_search_document_value(order)
        order.save()

        channel = order.channel
        channel_slug = channel.slug
        order_lines_info = []
        for line in order.lines.all():
            if line.variant.track_inventory or line.variant.is_preorder_active():
                line_data = OrderLineInfo(
                    line=line, quantity=line.quantity, variant=line.variant
                )
                order_lines_info.append(line_data)
                try:
                    with traced_atomic_transaction():
                        allocate_stocks(
                            [line_data],
                            country,
                            channel_slug,
                            manager,
                            check_reservations=is_reservation_enabled(
                                info.context.site.settings
                            ),
                        )
                        allocate_preorders(
                            [line_data],
                            channel_slug,
                            check_reservations=is_reservation_enabled(
                                info.context.site.settings
                            ),
                        )
                except InsufficientStock as exc:
                    errors = prepare_insufficient_stock_order_validation_errors(exc)
                    raise ValidationError({"lines": errors})

        order_info = OrderInfo(
            order=order,
            customer_email=order.get_customer_email(),
            channel=channel,
            payment=order.get_last_payment(),
            lines_data=order_lines_info,
        )

        order_created(
            order_info=order_info,
            user=info.context.user,
            app=info.context.app,
            manager=info.context.plugins,
            from_draft=True,
        )

        return DraftOrderComplete(order=order)
