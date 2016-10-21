# -*- coding: utf-8 -*-

import json
import logging
import threading
import multiprocessing
import sys
import time

import pika

logger = logging.getLogger(__name__)

class RabbitConsumer(object):
    """This is an example consumer that will handle unexpected interactions
    with RabbitMQ such as channel and connection closures.

    If RabbitMQ closes the connection, it will reopen it. You should
    look at the output, as there are limited reasons why the connection may
    be closed, which usually are tied to permission related issues or
    socket timeouts.

    If the channel is closed, it will indicate a problem with one of the
    commands that were issued and that should surface in the output as well.

    """

    DEFAULT_PREFETCH_COUNT = 1

    def __init__(self, bot_id, exchange, callback_func, rabbit_user, rabbit_pw, rabbit_host,
                 rabbit_port, consumer_id = 0, internal_error_queue = None, statsd = None):
        """Create a new instance of the consumer class, passing in the AMQP
        URL used to connect to RabbitMQ.

        :param str amqp_url: The AMQP url to connect with

        """

        super(RabbitConsumer, self).__init__()

        self.rabbit_port = rabbit_port
        self.rabbit_host = rabbit_host
        self.rabbit_pw = rabbit_pw
        self.rabbit_user = rabbit_user
        self.bot_id = bot_id
        self.exchange = exchange
        self.callback_func = callback_func
        self._closing = False
        self.stopped = False
        self._connection = None
        self._channel = None
        self._closing = False
        self._consumer_tag = None

        self.queue_name = self.exchange + "-" + self.bot_id
        self.error_queue_name = 'error-' + self.bot_id + "-" + self.exchange
        self.consumer_id = consumer_id
        self.internal_error_queue = internal_error_queue

        self.statsd = statsd

        self.statsd_prefix = self.exchange + "."

        self.invocations = 0
        self.total_execution_time = 0

    def connect(self):
        """This method connects to RabbitMQ, returning the connection handle.
        When the connection is established, the on_connection_open method
        will be invoked by pika.

        :rtype: pika.SelectConnection

        """
        logger.info("[{}]  Connecting to exchange {}".format(self.bot_id, self.exchange))
        creds = pika.PlainCredentials(self.rabbit_user, self.rabbit_pw)
        return pika.SelectConnection(pika.ConnectionParameters(host=self.rabbit_host,
                                                               port=self.rabbit_port,
                                                               virtual_host='/',
                                                               credentials=creds,
                                                               socket_timeout=1,
                                                               retry_delay=5  # 5 seconds
                                                               ),
                                     self.on_connection_open,
                                     stop_ioloop_on_close=False)

    def on_connection_open(self, unused_connection):
        """This method is called by pika once the connection to RabbitMQ has
        been established. It passes the handle to the connection object in
        case we need it, but in this case, we'll just mark it unused.

        :type unused_connection: pika.SelectConnection

        """
        logger.info('Connection opened')
        self.add_on_connection_close_callback()
        self.open_channel()

    def add_on_connection_close_callback(self):
        """This method adds an on close callback that will be invoked by pika
        when RabbitMQ closes the connection to the publisher unexpectedly.

        """
        logger.info('Adding connection close callback')
        self._connection.add_on_close_callback(self.on_connection_closed)

    def on_connection_closed(self, connection, reply_code, reply_text):
        """This method is invoked by pika when the connection to RabbitMQ is
        closed unexpectedly. Since it is unexpected, we will reconnect to
        RabbitMQ if it disconnects.

        :param pika.connection.Connection connection: The closed connection obj
        :param int reply_code: The server provided reply_code if given
        :param str reply_text: The server provided reply_text if given

        """
        self._channel = None
        if self._closing:
            self._connection.ioloop.stop()
        else:
            logger.warning('Connection closed, reopening in 5 seconds: (%s) %s',
                           reply_code, reply_text)
            self._connection.add_timeout(5, self.reconnect)

    def reconnect(self):
        """Will be invoked by the IOLoop timer if the connection is
        closed. See the on_connection_closed method.

        """
        # This is the old connection IOLoop instance, stop its ioloop
        self._connection.ioloop.stop()

        if not self._closing:
            # Create a new connection
            self._connection = self.connect()

            # There is now a new connection, needs a new ioloop to run
            self._connection.ioloop.start()

    def open_channel(self):
        """Open a new channel with RabbitMQ by issuing the Channel.Open RPC
        command. When RabbitMQ responds that the channel is open, the
        on_channel_open callback will be invoked by pika.

        """
        logger.info('Creating a new channel')
        self._connection.channel(on_open_callback=self.on_channel_open)

    def on_channel_open(self, channel):
        """This method is invoked by pika when the channel has been opened.
        The channel object is passed in so we can make use of it.

        Since the channel is now open, we'll declare the exchange to use.

        :param pika.channel.Channel channel: The channel object

        """
        logger.info('Channel opened')
        self._channel = channel
        self._channel.basic_qos(prefetch_count=
                                self.DEFAULT_PREFETCH_COUNT)
        self.add_on_channel_close_callback()
        self.setup_queues_and_bindings()

    def add_on_channel_close_callback(self):
        """This method tells pika to call the on_channel_closed method if
        RabbitMQ unexpectedly closes the channel.

        """
        logger.info('Adding channel close callback')
        self._channel.add_on_close_callback(self.on_channel_closed)

    def on_channel_closed(self, channel, reply_code, reply_text):
        """Invoked by pika when RabbitMQ unexpectedly closes the channel.
        Channels are usually closed if you attempt to do something that
        violates the protocol, such as re-declare an exchange or queue with
        different parameters. In this case, we'll close the connection
        to shutdown the object.

        :param pika.channel.Channel: The closed channel
        :param int reply_code: The numeric reason the channel was closed
        :param str reply_text: The text reason the channel was closed

        """
        logger.warning('Channel %i was closed: (%s) %s',
                       channel, reply_code, reply_text)
        self._connection.close()

    def setup_queues_and_bindings(self):
        """Check that the expected exchange is present on the server, only proceed if so
        """
        self._channel.exchange_declare(self.setup_queue, exchange=self.exchange, passive=True)

    def setup_queue(self, method_frame):
        """Setup the queue on RabbitMQ by invoking the Queue.Declare RPC
        command. When it is complete, the on_queue_declareok method will
        be invoked by pika.

        :param str|unicode queue_name: The name of the queue to declare.

        """
        logger.info('Declaring queue %s', self.queue_name)
        # self._channel.queue_declare(self.on_queue_declareok, queue_name)

        self._channel.queue_declare(self.on_queue_declareok, exclusive=False, durable=True, queue=self.queue_name)


    def on_queue_declareok(self, method_frame):
        """Method invoked by pika when the Queue.Declare RPC call made in
        setup_queue has completed. In this method we will bind the queue
        and exchange together with the routing key by issuing the Queue.Bind
        RPC command. When this command is complete, the on_bindok method will
        be invoked by pika.

        :param pika.frame.Method method_frame: The Queue.DeclareOk frame

        """
        # LOGGER.info('Binding %s to %s with %s',
        #             self.EXCHANGE, self.QUEUE, self.ROUTING_KEY)
        # self._channel.queue_bind(self.on_bindok, self.QUEUE,
        #                          self.EXCHANGE, self.ROUTING_KEY)
        logger.info(
            "[{}] Binding to {} with queue {} and routing key \"\"".format(self.bot_id, self.exchange,
                                                                            self.queue_name))

        self._channel.queue_bind(self.on_bindok,
                                 queue=self.queue_name,
                                 exchange=self.exchange,
                                 routing_key="")

    def on_bindok(self, unused_frame):
        """Invoked by pika when the Queue.Bind method has completed. At this
        point we will start consuming messages by calling start_consuming
        which will invoke the needed RPC commands to start the process.

        :param pika.frame.Method unused_frame: The Queue.BindOk response frame

        """
        logger.info('Queue bound')
        self.setup_error_queue()

    def setup_error_queue(self):
        """Setup the error queue on RabbitMQ by invoking the Queue.Declare RPC
        command.

        :param str|unicode queue_name: The name of the queue to declare.

        """
        logger.info('Declaring error queue %s', self.error_queue_name)

        self._channel.queue_declare(self.on_error_queue_declareok,queue=self.error_queue_name, durable=True, exclusive=False)

    def on_error_queue_declareok(self, method_frame):

        # LOGGER.info('Binding %s to %s with %s',
        #             self.EXCHANGE, self.QUEUE, self.ROUTING_KEY)
        # self._channel.queue_bind(self.on_bindok, self.QUEUE,
        #                          self.EXCHANGE, self.ROUTING_KEY)
        logger.info(
            "[{}] error queue created :{} \"\"".format(self.bot_id, self.error_queue_name))

        logger.info('STARTING CONSUMER')
        self.start_consuming()

    def start_consuming(self):
        """This method sets up the consumer by first calling
        add_on_cancel_callback so that the object is notified if RabbitMQ
        cancels the consumer. It then issues the Basic.Consume RPC command
        which returns the consumer tag that is used to uniquely identify the
        consumer with RabbitMQ. We keep the value to use it when we want to
        cancel consuming. The on_message method is passed in as a callback pika
        will invoke when a message is fully received.

        """
        logger.info('Issuing consumer related RPC commands')
        self.add_on_cancel_callback()
        logger.info("[{}]  Waiting for messages on exchange {}".format(self.bot_id, self.exchange))
        self._consumer_tag = self._channel.basic_consume(self.on_message,
                                                         self.queue_name)

    def add_on_cancel_callback(self):
        """Add a callback that will be invoked if RabbitMQ cancels the consumer
        for some reason. If RabbitMQ does cancel the consumer,
        on_consumer_cancelled will be invoked by pika.

        """
        logger.info('Adding consumer cancellation callback')
        self._channel.add_on_cancel_callback(self.on_consumer_cancelled)

    def on_consumer_cancelled(self, method_frame):
        """Invoked by pika when RabbitMQ sends a Basic.Cancel for a consumer
        receiving messages.

        :param pika.frame.Method method_frame: The Basic.Cancel frame

        """
        logger.info('Consumer was cancelled remotely, shutting down: %r',
                    method_frame)
        if self._channel:
            self._channel.close()

    def on_message(self, unused_channel, basic_deliver, properties, body):
        """Invoked by pika when a message is delivered from RabbitMQ. The
        channel is passed for your convenience. The basic_deliver object that
        is passed in carries the exchange, routing key, delivery tag and
        a redelivered flag for the message. The properties passed in is an
        instance of BasicProperties with the message properties and the body
        is the message that was sent.

        :param pika.channel.Channel unused_channel: The channel object
        :param pika.Spec.Basic.Deliver: basic_deliver method
        :param pika.Spec.BasicProperties: properties
        :param str|unicode body: The message body

        """

        start = time.time()
        self.invocations += 1

        logger.info(
            u"[{}] received message #{} from exchange {}: {}".format(self.bot_id,
                                                                      basic_deliver.delivery_tag, self.exchange,
                                                                      body.decode('utf-8')))

        self.statsd.incr(self.statsd_prefix + "message.receive")

        # Ack the message before processing to tell rabbit we got it.
        # TODO before sending ack we should persist the message in a local queue to avoid the possibility of losing it
        self.acknowledge_message(basic_deliver.delivery_tag)

        try:

            try:
                json_body = json.loads(body)

            except ValueError as ve:
                logger.exception(
                    "[{}] Invalid JSON received from exchange: {} error: {} msg body: []".format(self.bot_id,
                                                                                                  self.exchange,
                                                                                                  ve.message, body))
                raise

            else:
                response_messages = self.callback_func(json_body)

                if response_messages is None:
                    response_messages = []

                logger.info("[{}] Sending {} response messages".format(self.bot_id, len(response_messages)))

                for message in response_messages:
                    self._channel.basic_publish(exchange=message.get('exchange', self.exchange),
                                                routing_key=message.get('queue', self.queue_name),
                                                body=message.get('body'))
                    logger.info("[{}] published message {}".format(self.bot_id, message))
                    self.statsd.incr(self.statsd_prefix + "message.publish")

        except Exception as e:
            msg = "[{}] Unexpected error - {}, message {}, from exchange {}. sending to error queue {}"
            self.statsd.incr(self.statsd_prefix + "message.error")
            logger.exception(msg.format(self.bot_id, e, body, self.exchange, self.error_queue_name))
            self._channel.basic_publish(exchange='',
                                        routing_key=self.error_queue_name,
                                        body=body)


        exec_time_millis = int((time.time() - start) * 1000)
        self.total_execution_time += exec_time_millis

        logger.debug("Consumer {0} message handling time: {1}ms".format(self.consumer_id, exec_time_millis))

        # if we have processed 100 messages, log out the average execution time at INFO then reset the total
        if self.invocations % 100 == 0:
            average_execution_time = self.total_execution_time / 100
            logger.info("Consumer {0} Avg message handling time (last 100): {1}ms".format(self.consumer_id, average_execution_time))
            self.total_execution_time = 0

        self.statsd.timing(self.statsd_prefix + 'message.process.time', int((time.time() - start) * 1000))

    def acknowledge_message(self, delivery_tag):
        """Acknowledge the message delivery from RabbitMQ by sending a
        Basic.Ack RPC method for the delivery tag.

        :param int delivery_tag: The delivery tag from the Basic.Deliver frame

        """
        logger.info('Acknowledging message %s process %s consumer_id %s', delivery_tag, threading.current_thread, str(self.consumer_id))
        self._channel.basic_ack(delivery_tag)


    def stop_consuming(self):
        """Tell RabbitMQ that you would like to stop consuming by sending the
        Basic.Cancel RPC command.

        """
        if self._channel:
            logger.info('Sending a Basic.Cancel RPC command to RabbitMQ')
            self._channel.basic_cancel(self.on_cancelok, self._consumer_tag)

    def on_cancelok(self, unused_frame):
        """This method is invoked by pika when RabbitMQ acknowledges the
        cancellation of a consumer. At this point we will close the channel.
        This will invoke the on_channel_closed method once the channel has been
        closed, which will in-turn close the connection.

        :param pika.frame.Method unused_frame: The Basic.CancelOk frame

        """
        logger.info('RabbitMQ acknowledged the cancellation of the consumer')
        self.close_channel()

    def close_channel(self):
        """Call to close the channel with RabbitMQ cleanly by issuing the
        Channel.Close RPC command.

        """
        logger.info('Closing the channel')
        self._channel.close()

    def run(self):
        """Run the example consumer by connecting to RabbitMQ and then
        starting the IOLoop to block and allow the SelectConnection to operate.

        """
        try:

            self._connection = self.connect()
            self._connection.ioloop.start()
        except (KeyboardInterrupt, SystemExit):
            self.stop()
        except Exception as e:
            logger.warn("Exception: %s", str(e))
            logger.warn("Exception caught on rabbit consumer for process: %s with consumer id %s", threading.current_thread, str(self.consumer_id))
            self.internal_error_queue.put(self.consumer_id)

    def stop(self):
        """Cleanly shutdown the connection to RabbitMQ by stopping the consumer
        with RabbitMQ. When RabbitMQ confirms the cancellation, on_cancelok
        will be invoked by pika, which will then closing the channel and
        connection. The IOLoop is started again because this method is invoked
        when CTRL-C is pressed raising a KeyboardInterrupt exception. This
        exception stops the IOLoop which needs to be running for pika to
        communicate with RabbitMQ. All of the commands issued prior to starting
        the IOLoop will be buffered but not processed.

        """
        logger.info("Stopping rabbit consumer for process: %s with consumer id %s", threading.current_thread, str(self.consumer_id))
        self._closing = True
        self.stop_consuming()
        if self._connection is not None:
            self._connection.ioloop.start()
        self.stopped = True
        logger.info("Stopped rabbit consumer for process: %s with consumer id %s", threading.current_thread,
                    str(self.consumer_id))


    def close_connection(self):
        """This method closes the connection to RabbitMQ."""
        logger.info('Closing connection')
        self._connection.close()

class RabbitConsumerProc(RabbitConsumer, multiprocessing.Process):
    def start(self):
        super(RabbitConsumerProc, self).start()

    def total_stop(self):
        # super(RabbitConsumerProc, self).stop()
        super(RabbitConsumerProc, self).join(10)

class RabbitConsumerThread(RabbitConsumer, threading.Thread):
    def start(self):
        super(RabbitConsumerThread, self).start()

    def total_stop(self):
        super(RabbitConsumerThread, self).stop()
        # super(RabbitConsumerThread, self).join()